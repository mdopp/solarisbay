"""Jellyfin music ingest adapter (#564 slice 1, docs/okf-write-contract.md §6).

The Jellyfin client is mocked (no live server): a `FakeJellyfinMusicClient`
yields `JellyfinItem` dataclasses, so these cover the auth flow, the
MusicArtist→band / Audio→song OKF mapping, the `by` artist edge, and the
idempotent re-ingest skip — without touching the REST layer.

Schema is built from inlined DDL mirroring the #446 migration (importing alembic
from a solaris-chat test fails CI's clean env).
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator

import aiohttp
import pytest

from solaris_chat.engine.ingest import JellyfinMusicIngest
from solaris_chat.engine.ingest import jellyfin as jellyfin_mod
from solaris_chat.engine.ingest.jellyfin import JellyfinItem, RestJellyfinMusicClient
from solaris_chat.engine.knowledge import projection
from solaris_chat.engine.knowledge.writer import OkfWriter


# Mirrors database/migrations/versions/20260615_0016_okf_knowledge_index.py.
_SCHEMA = """
CREATE TABLE entities (
  id TEXT PRIMARY KEY, type TEXT NOT NULL, canonical_name TEXT NOT NULL,
  resident_uid TEXT NOT NULL, source TEXT NOT NULL, content_hash TEXT NOT NULL,
  updated TEXT NOT NULL DEFAULT (datetime('now')));
CREATE TABLE entity_aliases (
  entity_id TEXT NOT NULL, alias TEXT NOT NULL,
  PRIMARY KEY (entity_id, alias),
  FOREIGN KEY (entity_id) REFERENCES entities (id));
CREATE INDEX entity_aliases_alias_idx ON entity_aliases (alias);
CREATE TABLE facts (
  id TEXT PRIMARY KEY, subject_entity_id TEXT, resident_uid TEXT NOT NULL,
  predicate TEXT NOT NULL, value TEXT NOT NULL, confidence REAL,
  source TEXT NOT NULL, timestamp TEXT NOT NULL DEFAULT (datetime('now')),
  FOREIGN KEY (subject_entity_id) REFERENCES entities (id));
CREATE INDEX facts_subject_predicate_idx ON facts (subject_entity_id, predicate);
CREATE TABLE events (
  id TEXT PRIMARY KEY, ts TEXT NOT NULL, resident_uid TEXT NOT NULL,
  kind TEXT NOT NULL, source TEXT NOT NULL);
CREATE INDEX events_ts_idx ON events (ts);
CREATE INDEX events_resident_ts_idx ON events (resident_uid, ts);
CREATE TABLE event_entities (
  event_id TEXT NOT NULL, entity_id TEXT NOT NULL, role TEXT NOT NULL,
  PRIMARY KEY (event_id, entity_id, role),
  FOREIGN KEY (event_id) REFERENCES events (id),
  FOREIGN KEY (entity_id) REFERENCES entities (id));
CREATE TABLE concepts (
  id TEXT PRIMARY KEY, ref_id TEXT NOT NULL,
  ref_kind TEXT NOT NULL CHECK (ref_kind IN ('entity', 'event')),
  okf_path TEXT NOT NULL, embedding_id TEXT, content_hash TEXT NOT NULL,
  updated TEXT NOT NULL DEFAULT (datetime('now')));
CREATE TABLE ingest_log (
  source TEXT NOT NULL, external_id TEXT NOT NULL, content_hash TEXT NOT NULL,
  ingested_at TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (source, external_id));
CREATE INDEX ingest_log_source_external_idx ON ingest_log (source, external_id);
"""


class FakeJellyfinMusicClient:
    """A mocked read-only Jellyfin source.

    Single-library form: pass a flat `list[JellyfinItem]` (one default 'Music'
    library). Multi-library form: pass `libraries=[(id, name)]` and
    `by_library={lib_id: [items]}` to exercise per-library ownership (#576).
    """

    def __init__(
        self,
        items: list[JellyfinItem] | None = None,
        *,
        libraries: list[tuple[str, str]] | None = None,
        by_library: dict[str, list[JellyfinItem]] | None = None,
    ):
        self.authenticated = 0
        if libraries is not None:
            self._libraries = libraries
            self._by_library = by_library or {}
        else:
            self._libraries = [("lib-music", "Music")]
            self._by_library = {"lib-music": items or []}

    async def authenticate(self) -> None:
        self.authenticated += 1

    async def libraries(self) -> list[tuple[str, str]]:
        await self.authenticate()
        return self._libraries

    async def iter_library(self, library_id: str) -> AsyncIterator[JellyfinItem]:
        await self.authenticate()
        for item in self._by_library.get(library_id, []):
            yield item

    def audio_uri(self, item_id: str) -> str:
        return f"jellyfin://audio/{item_id}"


@pytest.fixture
def env(tmp_path):
    db_path = str(tmp_path / "solaris.db")
    notes_dir = str(tmp_path / "notes")
    conn = sqlite3.connect(db_path)
    conn.executescript(_SCHEMA)
    conn.commit()
    conn.close()
    writer = OkfWriter(db_path=db_path, notes_dir=notes_dir)
    return writer, db_path, tmp_path


def _artist(**kw) -> JellyfinItem:
    base = dict(id="ar1", kind="MusicArtist", name="Queen", changed="2026-05-01")
    base.update(kw)
    return JellyfinItem(**base)


def _track(**kw) -> JellyfinItem:
    base = dict(
        id="t1",
        kind="Audio",
        name="Bohemian Rhapsody",
        artist="Queen",
        album="A Night at the Opera",
        genre="Rock",
        year="1975",
        changed="2026-05-02",
    )
    base.update(kw)
    return JellyfinItem(**base)


def _run(client, writer, *, uid="household", library_owners=None):
    ingest = JellyfinMusicIngest(
        client, writer, ingesting_uid=uid, library_owners=library_owners
    )
    return asyncio.run(ingest.run())


# --- auth flow ---------------------------------------------------------------


def test_run_authenticates_before_ingesting(env):
    writer, _, _ = env
    client = FakeJellyfinMusicClient([_artist()])
    _run(client, writer)
    # run() authenticates, then iter_music authenticates (idempotent on the
    # REST client); the fake just records both calls.
    assert client.authenticated >= 1


# --- artist -> band / track -> song mapping ----------------------------------


def test_artist_maps_to_band_concept(env):
    writer, db_path, tmp_path = env
    stats = _run(FakeJellyfinMusicClient([_artist()]), writer)
    assert stats.items == 1 and stats.bands_written == 1
    conn = projection.open_conn(db_path)
    band = conn.execute("SELECT * FROM entities WHERE type = 'band'").fetchone()
    assert band["canonical_name"] == "Queen"
    conn.close()
    band_path = tmp_path / "notes" / "okf" / "bands" / "queen.md"
    assert band_path.is_file()


def test_artist_writes_genre_and_bio_facts(env):
    # A MusicArtist carrying Genres + Overview projects genre/bio FACTS on the
    # band concept (#592) — so artist_info('Queen') can surface them.
    writer, db_path, tmp_path = env
    artist = _artist(genres="Rock, Pop", overview="British rock band formed in 1970.")
    _run(FakeJellyfinMusicClient([artist]), writer)
    conn = projection.open_conn(db_path)
    band = conn.execute("SELECT id FROM entities WHERE type = 'band'").fetchone()
    facts = {
        r["predicate"]: r["value"]
        for r in conn.execute(
            "SELECT predicate, value FROM facts WHERE subject_entity_id = ?",
            (band["id"],),
        )
    }
    conn.close()
    assert facts["genre"] == "Rock, Pop"
    assert facts["bio"] == "British rock band formed in 1970."
    band_text = (tmp_path / "notes" / "okf" / "bands" / "queen.md").read_text()
    assert "genre: Rock, Pop" in band_text
    assert "bio: British rock band formed in 1970." in band_text


def test_artist_enrichment_survives_prior_bare_track_write(env):
    # A track (no enrichment) writing the band first must NOT block the later
    # MusicArtist write that carries genre/bio.
    writer, db_path, _ = env
    track = _track()  # writes the 'Queen' band bare
    artist = _artist(genres="Rock", overview="Bio.")
    _run(FakeJellyfinMusicClient([track, artist]), writer)
    conn = projection.open_conn(db_path)
    band = conn.execute("SELECT id FROM entities WHERE type = 'band'").fetchone()
    facts = {
        r["predicate"]
        for r in conn.execute(
            "SELECT predicate FROM facts WHERE subject_entity_id = ? AND predicate"
            " IN ('genre', 'bio')",
            (band["id"],),
        )
    }
    conn.close()
    assert facts == {"genre", "bio"}


def test_track_maps_to_projection_only_song_with_by_edge(env):
    # A song is projection-only (ADR 0002/0005): an entity + facts, NO per-song
    # OKF markdown. Its `by`/`on_album`/`resource` live as facts, not a file.
    writer, db_path, tmp_path = env
    stats = _run(FakeJellyfinMusicClient([_track()]), writer)
    assert stats.songs_written == 1
    # The track's artist is written as a band too (so the `by` edge resolves).
    assert stats.bands_written == 1
    conn = projection.open_conn(db_path)
    song = conn.execute("SELECT * FROM entities WHERE type = 'song'").fetchone()
    assert song["canonical_name"] == "Bohemian Rhapsody"
    facts = {
        r["predicate"]: r["value"]
        for r in conn.execute(
            "SELECT predicate, value FROM facts WHERE subject_entity_id = ?",
            (song["id"],),
        )
    }
    conn.close()
    assert facts["by"] == "bands/queen"
    assert facts["on_album"].startswith("albums/")
    assert facts["resource"] == "jellyfin://audio/t1"
    # No per-song markdown and no `concepts` link row for the song.
    assert not (tmp_path / "notes" / "okf" / "songs").exists()
    conn = projection.open_conn(db_path)
    song_concepts = conn.execute(
        "SELECT COUNT(*) FROM concepts c JOIN entities e ON c.ref_id = e.id"
        " WHERE e.type = 'song'"
    ).fetchone()[0]
    conn.close()
    assert song_concepts == 0


def test_track_without_artist_writes_song_without_band(env):
    writer, db_path, _ = env
    stats = _run(FakeJellyfinMusicClient([_track(artist="")]), writer)
    assert stats.songs_written == 1 and stats.bands_written == 0
    conn = projection.open_conn(db_path)
    assert (
        conn.execute("SELECT COUNT(*) FROM entities WHERE type = 'band'").fetchone()[0]
        == 0
    )
    conn.close()


def test_shared_artist_band_dedups_across_tracks(env):
    writer, db_path, _ = env
    t1 = _track(id="t1", name="One", changed="c1")
    t2 = _track(id="t2", name="Two", changed="c2")
    stats = _run(FakeJellyfinMusicClient([t1, t2]), writer)
    # Two tracks by Queen -> two songs, ONE band written.
    assert stats.songs_written == 2 and stats.bands_written == 1
    conn = projection.open_conn(db_path)
    assert (
        conn.execute("SELECT COUNT(*) FROM entities WHERE type = 'band'").fetchone()[0]
        == 1
    )
    conn.close()


def test_music_catalog_is_household_scoped(env):
    writer, db_path, _ = env
    # No library_owners map -> the single 'Music' library is shared.
    _run(FakeJellyfinMusicClient([_track()]), writer, uid="mdopp")
    conn = projection.open_conn(db_path)
    scopes = {r[0] for r in conn.execute("SELECT DISTINCT resident_uid FROM entities")}
    assert scopes == {"household"}
    conn.close()


# --- per-library ownership (#576) --------------------------------------------


def test_private_library_writes_under_owner_path(env):
    writer, db_path, tmp_path = env
    client = FakeJellyfinMusicClient(
        libraries=[("lib-c", "Music (cdopp)")],
        by_library={"lib-c": [_track(id="t1", name="Geheim", artist="Adele")]},
    )
    _run(client, writer, library_owners={"Music (cdopp)": "cdopp"})
    conn = projection.open_conn(db_path)
    scopes = {r[0] for r in conn.execute("SELECT DISTINCT resident_uid FROM entities")}
    assert scopes == {"cdopp"}
    conn.close()
    # cdopp's materialized concepts (album/artist — songs are projection-only)
    # live under her private path, not the shared okf/ root.
    assert list(
        (tmp_path / "notes" / "users" / "cdopp" / "okf" / "albums").glob("*.md")
    )
    assert not (tmp_path / "notes" / "okf" / "albums").exists()
    # Songs never materialize markdown at all (projection-only).
    assert not (tmp_path / "notes" / "users" / "cdopp" / "okf" / "songs").exists()
    assert not (tmp_path / "notes" / "okf" / "songs").exists()


def test_library_name_case_insensitive_owner_match(env):
    writer, db_path, _ = env
    client = FakeJellyfinMusicClient(
        libraries=[("lib-c", "music (CDOPP)")],
        by_library={"lib-c": [_artist(name="Adele")]},
    )
    _run(client, writer, library_owners={"Music (cdopp)": "cdopp"})
    conn = projection.open_conn(db_path)
    scopes = {r[0] for r in conn.execute("SELECT DISTINCT resident_uid FROM entities")}
    assert scopes == {"cdopp"}
    conn.close()


def test_shared_and_private_libraries_split_by_owner(env):
    writer, db_path, tmp_path = env
    client = FakeJellyfinMusicClient(
        libraries=[("lib-s", "Music"), ("lib-c", "Music (cdopp)")],
        by_library={
            "lib-s": [_track(id="s1", name="Shared", artist="Beatles")],
            "lib-c": [_track(id="c1", name="Private", artist="Adele")],
        },
    )
    _run(client, writer, library_owners={"Music (cdopp)": "cdopp"})
    conn = projection.open_conn(db_path)
    rows = dict(
        conn.execute(
            "SELECT canonical_name, resident_uid FROM entities WHERE type = 'song'"
        ).fetchall()
    )
    assert rows == {"Shared": "household", "Private": "cdopp"}
    conn.close()
    # Songs are projection-only; the materialized markdown split is on albums.
    assert (tmp_path / "notes" / "okf" / "albums").exists()
    assert (tmp_path / "notes" / "users" / "cdopp" / "okf" / "albums").exists()
    assert not (tmp_path / "notes" / "okf" / "songs").exists()
    assert not (tmp_path / "notes" / "users" / "cdopp" / "okf" / "songs").exists()


def test_band_in_both_libraries_stays_shared(env):
    # An artist appearing in a shared library AND cdopp's private library must
    # stay household — shared artists stay shared (operator rule).
    writer, db_path, _ = env
    client = FakeJellyfinMusicClient(
        libraries=[("lib-s", "Music"), ("lib-c", "Music (cdopp)")],
        by_library={
            "lib-s": [_track(id="s1", name="Shared Hit", artist="Queen")],
            "lib-c": [_track(id="c1", name="Private Take", artist="Queen")],
        },
    )
    _run(client, writer, library_owners={"Music (cdopp)": "cdopp"})
    conn = projection.open_conn(db_path)
    band = conn.execute(
        "SELECT resident_uid FROM entities WHERE type = 'band'"
    ).fetchone()
    assert band["resident_uid"] == "household"
    conn.close()


# --- album as a first-class entity (#876) ------------------------------------


def test_track_writes_album_entity_and_join_facts(env):
    # A track projects an `album` entity plus the two join facts: song→on_album→
    # album and album→by→artist (the band node, reused, not a parallel artist).
    writer, db_path, tmp_path = env
    stats = _run(FakeJellyfinMusicClient([_track()]), writer)
    assert stats.albums_written == 1
    conn = projection.open_conn(db_path)
    album = conn.execute("SELECT * FROM entities WHERE type = 'album'").fetchone()
    assert album is not None
    # canonical_name carries the artist so the dedup key is (artist, album).
    assert album["canonical_name"] == "Queen – A Night at the Opera"
    song = conn.execute("SELECT id FROM entities WHERE type = 'song'").fetchone()
    on_album = conn.execute(
        "SELECT value FROM facts WHERE predicate = 'on_album' AND subject_entity_id = ?",
        (song["id"],),
    ).fetchone()
    assert on_album is not None and on_album["value"].startswith("albums/")
    album_by = conn.execute(
        "SELECT value FROM facts WHERE predicate = 'by' AND subject_entity_id = ?",
        (album["id"],),
    ).fetchone()
    assert album_by is not None and album_by["value"] == "bands/queen"
    conn.close()
    # source=jellyfin on every projected fact.
    conn = projection.open_conn(db_path)
    srcs = {r[0] for r in conn.execute("SELECT DISTINCT source FROM facts")}
    conn.close()
    assert srcs == {"jellyfin"}
    assert (
        tmp_path / "notes" / "okf" / "albums" / "queen-a-night-at-the-opera.md"
    ).is_file()


def test_album_and_artist_keep_markdown_and_embedding_song_does_not(env):
    # ADR 0005: the RAG surface for the library is album/artist, so those keep a
    # markdown file AND an embedding enqueue; the song (projection-only) gets
    # neither. Prove the enqueue granularity via a spy queue.
    _, db_path, tmp_path = env

    class _SpyQueue:
        def __init__(self):
            self.calls: list[str] = []

        def enqueue(self, *, concept_id, embedding_id, text):
            self.calls.append(concept_id)
            return embedding_id

    spy = _SpyQueue()
    writer = OkfWriter(
        db_path=db_path, notes_dir=str(tmp_path / "notes"), embedding_queue=spy
    )
    _run(FakeJellyfinMusicClient([_track()]), writer)

    conn = projection.open_conn(db_path)
    ids_by_type = {
        r["type"]: r["id"] for r in conn.execute("SELECT id, type FROM entities")
    }
    conn.close()
    # Album + artist(band) were enqueued for embedding; the song was NOT.
    assert ids_by_type["album"] in spy.calls
    assert ids_by_type["band"] in spy.calls
    assert ids_by_type["song"] not in spy.calls
    # And only the RAG-worthy nodes materialized markdown (no songs/ dir).
    assert (tmp_path / "notes" / "okf" / "albums").is_dir()
    assert (tmp_path / "notes" / "okf" / "bands").is_dir()
    assert not (tmp_path / "notes" / "okf" / "songs").exists()


def test_album_dedups_across_tracks_and_reingest(env):
    # Two tracks from the same album -> ONE album node; re-ingesting the same
    # catalog creates NO duplicate album (dedup on artist+title).
    writer, db_path, _ = env
    t1 = _track(id="t1", name="One", changed="c1")
    t2 = _track(id="t2", name="Two", changed="c2")
    stats = _run(FakeJellyfinMusicClient([t1, t2]), writer)
    assert stats.albums_written == 1
    conn = projection.open_conn(db_path)
    assert (
        conn.execute("SELECT COUNT(*) FROM entities WHERE type = 'album'").fetchone()[0]
        == 1
    )
    conn.close()
    # Re-ingest: no new album node.
    _run(FakeJellyfinMusicClient([t1, t2]), writer)
    conn = projection.open_conn(db_path)
    assert (
        conn.execute("SELECT COUNT(*) FROM entities WHERE type = 'album'").fetchone()[0]
        == 1
    )
    conn.close()


def test_same_album_title_different_artists_are_distinct(env):
    # Two artists with an identically titled album must NOT merge into one node.
    writer, db_path, _ = env
    a = _track(id="a1", name="X", artist="Queen", album="Greatest Hits")
    b = _track(id="b1", name="Y", artist="Abba", album="Greatest Hits")
    _run(FakeJellyfinMusicClient([a, b]), writer)
    conn = projection.open_conn(db_path)
    assert (
        conn.execute("SELECT COUNT(*) FROM entities WHERE type = 'album'").fetchone()[0]
        == 2
    )
    conn.close()


def test_track_without_album_writes_no_album(env):
    writer, db_path, _ = env
    _run(FakeJellyfinMusicClient([_track(album="")]), writer)
    conn = projection.open_conn(db_path)
    assert (
        conn.execute("SELECT COUNT(*) FROM entities WHERE type = 'album'").fetchone()[0]
        == 0
    )
    conn.close()


def test_album_in_both_libraries_stays_shared(env):
    # Like bands: an album appearing in a shared AND a private library stays
    # household (shared albums stay shared).
    writer, db_path, _ = env
    client = FakeJellyfinMusicClient(
        libraries=[("lib-s", "Music"), ("lib-c", "Music (cdopp)")],
        by_library={
            "lib-s": [_track(id="s1", name="Shared Hit", artist="Queen")],
            "lib-c": [_track(id="c1", name="Private Take", artist="Queen")],
        },
    )
    _run(client, writer, library_owners={"Music (cdopp)": "cdopp"})
    conn = projection.open_conn(db_path)
    album = conn.execute(
        "SELECT resident_uid FROM entities WHERE type = 'album'"
    ).fetchone()
    assert album["resident_uid"] == "household"
    conn.close()


def test_private_library_album_scoped_to_owner(env):
    writer, db_path, _ = env
    client = FakeJellyfinMusicClient(
        libraries=[("lib-c", "Music (cdopp)")],
        by_library={"lib-c": [_track(id="t1", name="Geheim", artist="Adele")]},
    )
    _run(client, writer, library_owners={"Music (cdopp)": "cdopp"})
    conn = projection.open_conn(db_path)
    scopes = {
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT resident_uid FROM entities WHERE type = 'album'"
        )
    }
    conn.close()
    assert scopes == {"cdopp"}


# --- idempotent --------------------------------------------------------------


def test_reingest_unchanged_is_skipped(env):
    writer, db_path, _ = env
    _run(FakeJellyfinMusicClient([_track()]), writer)
    stats = _run(FakeJellyfinMusicClient([_track()]), writer)
    # The song re-ingest is skipped; the band re-ingest is short-circuited
    # in-run (seen) and also a no-op at the writer.
    assert stats.songs_written == 0 and stats.skipped == 1
    conn = projection.open_conn(db_path)
    assert (
        conn.execute("SELECT COUNT(*) FROM entities WHERE type = 'song'").fetchone()[0]
        == 1
    )
    conn.close()


def test_changed_metadata_reingests(env):
    writer, db_path, _ = env
    _run(FakeJellyfinMusicClient([_track(genre="Rock")]), writer)
    stats = _run(FakeJellyfinMusicClient([_track(genre="Pop")]), writer)
    # New genre rides the body -> content_hash moves -> not skipped, no dup.
    assert stats.songs_written == 1
    conn = projection.open_conn(db_path)
    assert (
        conn.execute("SELECT COUNT(*) FROM entities WHERE type = 'song'").fetchone()[0]
        == 1
    )
    conn.close()


def test_run_returns_high_water_cursor(env):
    writer, _, _ = env
    a = _track(id="t1", name="One", changed="2026-05-01T00:00:00")
    b = _track(id="t2", name="Two", changed="2026-05-30T10:00:00")
    stats = _run(FakeJellyfinMusicClient([a, b]), writer)
    assert stats.cursor == "2026-05-30T10:00:00"


# --- REST client: user-scoped library enumeration (#581) ---------------------


class _Resp:
    """A fake aiohttp response context manager.

    `raise_on_enter=True` makes the GET raise `ClientResponseError(status=401)`
    on entry, replicating a Jellyfin/proxy chain that surfaces the 401 as a
    RAISED error (raise-on-status) rather than a returned `resp.status` — the
    real path u78's re-auth never fired on (#583)."""

    request_info = None
    history = ()

    def __init__(self, *, json_body=None, status=200, raise_on_enter=False):
        self._json = json_body
        self.status = status
        self._raise_on_enter = raise_on_enter

    async def __aenter__(self):
        if self._raise_on_enter:
            raise aiohttp.ClientResponseError(None, (), status=self.status)
        return self

    async def __aexit__(self, *a):
        return False

    def raise_for_status(self):
        if self.status >= 400:
            raise aiohttp.ClientResponseError(None, (), status=self.status)

    async def json(self):
        return self._json


_VIEWS = {
    "Items": [
        {"Id": "v-music", "Name": "Music", "CollectionType": "music"},
        {"Id": "v-cdopp", "Name": "Music (cdopp)", "CollectionType": "music"},
        {"Id": "v-music2", "Name": "Music2", "CollectionType": "music"},
        {"Id": "v-playlists", "Name": "Playlists", "CollectionType": "playlists"},
        {"Id": "v-movies", "Name": "Filme", "CollectionType": "movies"},
    ]
}


def test_libraries_uses_user_views_not_admin_mediafolders(monkeypatch):
    # The read-only service user gets 403 on the admin /Library/MediaFolders but
    # 200 on the user-scoped /Users/{userId}/Views (#581). libraries() must keep
    # only the music collections (Playlists / non-music excluded).
    requested: list[str] = []

    class _Session:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def post(self, url, *, json=None, headers=None, **k):
            return _Resp(json_body={"AccessToken": "tok", "User": {"Id": "u-solaris"}})

        def get(self, url, *, headers=None, **k):
            requested.append(url)
            if "/Library/MediaFolders" in url:
                return _Resp(status=403)
            if "/Users/u-solaris/Views" in url:
                return _Resp(json_body=_VIEWS)
            raise AssertionError(f"unexpected GET {url}")

    monkeypatch.setattr(jellyfin_mod.aiohttp, "ClientSession", _Session)
    client = RestJellyfinMusicClient("http://jf", "solaris", "pw")
    libs = asyncio.run(client.libraries())

    assert not any("/Library/MediaFolders" in u for u in requested)
    assert any("/Users/u-solaris/Views" in u for u in requested)
    assert libs == [
        ("v-music", "Music"),
        ("v-cdopp", "Music (cdopp)"),
        ("v-music2", "Music2"),
    ]


def test_libraries_through_run_maps_per_library_owner(monkeypatch):
    # End-to-end via the REST client: Views returns the music libs (no admin),
    # the per-library owner mapping still applies (Music (cdopp) -> cdopp).
    def _items(name):
        return {
            "Items": [
                {
                    "Id": f"{name}-t",
                    "Type": "Audio",
                    "Name": f"{name} song",
                    "Artists": ["Adele" if "cdopp" in name else "Beatles"],
                }
            ],
            "TotalRecordCount": 1,
        }

    class _Session:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def post(self, url, *, json=None, headers=None, **k):
            return _Resp(json_body={"AccessToken": "tok", "User": {"Id": "u1"}})

        def get(self, url, *, headers=None, params=None, **k):
            if "/Library/MediaFolders" in url:
                return _Resp(status=403)
            if "/Users/u1/Views" in url:
                return _Resp(json_body=_VIEWS)
            if "/Items" in url:
                pid = (params or {}).get("ParentId", "")
                return _Resp(json_body=_items(pid))
            raise AssertionError(f"unexpected GET {url}")

    monkeypatch.setattr(jellyfin_mod.aiohttp, "ClientSession", _Session)
    # tmp db/notes via a fresh writer
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        db_path = f"{d}/solaris.db"
        conn = sqlite3.connect(db_path)
        conn.executescript(_SCHEMA)
        conn.commit()
        conn.close()
        writer = OkfWriter(db_path=db_path, notes_dir=f"{d}/notes")
        client = RestJellyfinMusicClient("http://jf", "solaris", "pw")
        ingest = JellyfinMusicIngest(
            client,
            writer,
            ingesting_uid="household",
            library_owners={"Music (cdopp)": "cdopp"},
        )
        asyncio.run(ingest.run())
        conn = projection.open_conn(db_path)
        rows = dict(
            conn.execute(
                "SELECT canonical_name, resident_uid FROM entities WHERE type = 'song'"
            ).fetchall()
        )
        conn.close()
    # All 3 music libs ingested (Playlists/movies excluded by libraries());
    # only cdopp's library is private.
    assert rows == {
        "v-music song": "household",
        "v-music2 song": "household",
        "v-cdopp song": "cdopp",
    }


def test_empty_slug_name_is_captured_via_item_id(env):
    # A track whose title slugifies empty (purely non-Latin) used to abort/skip
    # (#583); now it falls back to an id-based slug so nothing is lost. Both
    # tracks ingest, and the unusual one is reachable under its id-based slug.
    writer, db_path, tmp_path = env
    bad = _track(id="bad", name="王芳", artist="")
    ok = _track(id="ok", name="Good Song", artist="")
    stats = _run(FakeJellyfinMusicClient([bad, ok]), writer, library_owners=None)
    assert stats.items == 2 and stats.skipped == 0 and stats.songs_written == 2
    conn = projection.open_conn(db_path)
    # The empty-name track is captured under an id-based fallback slug at the
    # _write_song throw site (the `item-<id>-<id>` form), not lost to a
    # ValueError; both songs become projection rows (no per-song markdown).
    rows = {
        r["canonical_name"]: r["source"]
        for r in conn.execute("SELECT canonical_name, source FROM entities")
    }
    conn.close()
    assert rows["王芳"] == "jellyfin" and rows["Good Song"] == "jellyfin"
    assert not (tmp_path / "notes" / "okf" / "songs").exists()


def test_empty_slug_artist_band_and_by_edge_share_id_slug(env):
    # An artist string that slugifies empty: the band concept and the song's `by`
    # edge must share the same id-based fallback slug so the link still resolves.
    writer, db_path, _ = env
    track = _track(id="tk1", name="Song One", artist="王芳")
    stats = _run(FakeJellyfinMusicClient([track]), writer)
    assert stats.skipped == 0 and stats.bands_written == 1 and stats.songs_written == 1
    conn = projection.open_conn(db_path)
    bands = conn.execute(
        "SELECT COUNT(*) FROM entities WHERE type = 'band'"
    ).fetchone()[0]
    conn.close()
    assert bands == 1


def _items_page(items, *, total):
    return {"Items": items, "TotalRecordCount": total}


def test_iter_library_reauths_on_401_and_resumes_all_items(monkeypatch):
    # A token expires mid-pagination: the second /Items page 401s once, then 200s
    # after a re-auth. The iteration must re-authenticate and resume from the same
    # StartIndex so NO items are lost (the #583 truncation bug).
    auth_calls = {"n": 0}
    page_calls = {"n": 0}

    def _page(start):
        # 2 items per page, 3 items total -> two pages (start 0, start 2).
        if start == 0:
            return _items_page(
                [
                    {"Id": "i0", "Type": "Audio", "Name": "S0", "Artists": ["A"]},
                    {"Id": "i1", "Type": "Audio", "Name": "S1", "Artists": ["A"]},
                ],
                total=3,
            )
        return _items_page(
            [{"Id": "i2", "Type": "Audio", "Name": "S2", "Artists": ["A"]}], total=3
        )

    class _Session:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def post(self, url, *, json=None, headers=None, **k):
            auth_calls["n"] += 1
            return _Resp(
                json_body={"AccessToken": f"tok{auth_calls['n']}", "User": {"Id": "u1"}}
            )

        def get(self, url, *, headers=None, params=None, **k):
            start = int((params or {}).get("StartIndex", "0"))
            page_calls["n"] += 1
            # The 2nd /Items page 401s exactly once (token expired mid-ingest).
            if start == 2 and page_calls["n"] == 2:
                return _Resp(status=401)
            return _Resp(json_body=_page(start))

    monkeypatch.setattr(jellyfin_mod.aiohttp, "ClientSession", _Session)
    client = RestJellyfinMusicClient("http://jf", "solaris", "pw")

    async def _collect():
        return [it.id async for it in client.iter_library("lib-music")]

    ids = asyncio.run(_collect())

    assert ids == ["i0", "i1", "i2"]  # resumed, nothing lost
    assert auth_calls["n"] == 2  # initial auth + one re-auth on the 401
    assert client._token == "tok2"  # fresh token in use after re-auth


def test_iter_library_reauths_when_401_is_raised_mid_pagination(monkeypatch):
    # The REAL #583 path: the mid-pagination 401 surfaces as a RAISED
    # aiohttp.ClientResponseError(status=401) (raise-on-status in the client/proxy
    # chain), not a returned resp.status. u78 only checked resp.status==401, so the
    # exception propagated uncaught -> NO re-auth, truncated catalog. The fix must
    # catch the raised 401, re-auth, retry the SAME page with the fresh token, and
    # lose no items. This test FAILS against u78 and passes with the fix.
    auth_calls = {"n": 0}
    page_calls = {"n": 0}

    def _page(start):
        if start == 0:
            return _items_page(
                [
                    {"Id": "i0", "Type": "Audio", "Name": "S0", "Artists": ["A"]},
                    {"Id": "i1", "Type": "Audio", "Name": "S1", "Artists": ["A"]},
                ],
                total=3,
            )
        return _items_page(
            [{"Id": "i2", "Type": "Audio", "Name": "S2", "Artists": ["A"]}], total=3
        )

    class _Session:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def post(self, url, *, json=None, headers=None, **k):
            auth_calls["n"] += 1
            return _Resp(
                json_body={"AccessToken": f"tok{auth_calls['n']}", "User": {"Id": "u1"}}
            )

        def get(self, url, *, headers=None, params=None, **k):
            start = int((params or {}).get("StartIndex", "0"))
            page_calls["n"] += 1
            # The 2nd /Items page RAISES a 401 exactly once (token expired).
            if start == 2 and page_calls["n"] == 2:
                return _Resp(status=401, raise_on_enter=True)
            return _Resp(json_body=_page(start))

    monkeypatch.setattr(jellyfin_mod.aiohttp, "ClientSession", _Session)
    client = RestJellyfinMusicClient("http://jf", "solaris", "pw")

    async def _collect():
        return [it.id async for it in client.iter_library("lib-music")]

    ids = asyncio.run(_collect())

    assert ids == ["i0", "i1", "i2"]  # raised-401 caught -> resumed, nothing lost
    assert auth_calls["n"] == 2  # initial auth + one re-auth
    assert client._token == "tok2"  # retried with the fresh token


def test_iter_library_reauths_when_raised_401_recurs(monkeypatch):
    # A catalog longer than several token lifetimes: the raised 401 recurs on a
    # later page too. Re-auth must fire EACH time (up to _MAX_REAUTH), not just
    # once, so the whole tail ingests.
    auth_calls = {"n": 0}

    def _page(start):
        items = {
            0: [
                {"Id": "i0", "Type": "Audio", "Name": "S0", "Artists": ["A"]},
                {"Id": "i1", "Type": "Audio", "Name": "S1", "Artists": ["A"]},
            ],
            2: [
                {"Id": "i2", "Type": "Audio", "Name": "S2", "Artists": ["A"]},
                {"Id": "i3", "Type": "Audio", "Name": "S3", "Artists": ["A"]},
            ],
            4: [{"Id": "i4", "Type": "Audio", "Name": "S4", "Artists": ["A"]}],
        }[start]
        return _items_page(items, total=5)

    class _Session:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def post(self, url, *, json=None, headers=None, **k):
            auth_calls["n"] += 1
            return _Resp(
                json_body={"AccessToken": f"tok{auth_calls['n']}", "User": {"Id": "u1"}}
            )

        def get(self, url, *, headers=None, params=None, **k):
            start = int((params or {}).get("StartIndex", "0"))
            token = (headers or {}).get("X-Emby-Token", "")
            # tok1 expires by page 2; the re-authed tok2 expires by page 4. Each
            # forces a fresh re-auth, then the new token sails past that page.
            stale = (start == 2 and token == "tok1") or (start == 4 and token == "tok2")
            if stale:
                return _Resp(status=401, raise_on_enter=True)
            return _Resp(json_body=_page(start))

    monkeypatch.setattr(jellyfin_mod.aiohttp, "ClientSession", _Session)
    client = RestJellyfinMusicClient("http://jf", "solaris", "pw")

    async def _collect():
        return [it.id async for it in client.iter_library("lib-music")]

    ids = asyncio.run(_collect())

    assert ids == ["i0", "i1", "i2", "i3", "i4"]  # both expiries recovered
    assert auth_calls["n"] == 3  # initial + one re-auth per expired page


def test_get_json_reauth_is_bounded(monkeypatch):
    # A server that 401s every request must not loop forever: after the bounded
    # re-auth the request finally raises instead of re-authing endlessly.
    auth_calls = {"n": 0}

    class _Session:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def post(self, url, *, json=None, headers=None, **k):
            auth_calls["n"] += 1
            return _Resp(json_body={"AccessToken": "tok", "User": {"Id": "u1"}})

        def get(self, url, *, headers=None, params=None, **k):
            return _Resp(status=401)  # always unauthorized

    monkeypatch.setattr(jellyfin_mod.aiohttp, "ClientSession", _Session)
    client = RestJellyfinMusicClient("http://jf", "solaris", "pw")

    async def _collect():
        return [it async for it in client.iter_library("lib-music")]

    with pytest.raises(aiohttp.ClientResponseError):
        asyncio.run(_collect())
    # One initial auth + at most _MAX_REAUTH forced re-auths, never unbounded.
    assert auth_calls["n"] <= client._MAX_REAUTH + 1


# ---- lyrics() — on-demand live lyrics (#593) --------------------------------


def test_lyric_text_joins_timed_lines():
    payload = {
        "Lyrics": [
            {"Text": "Is this the real life?", "Start": 0},
            {"Text": "Is this just fantasy?", "Start": 30000000},
        ]
    }
    assert jellyfin_mod._lyric_text(payload) == (
        "Is this the real life?\nIs this just fantasy?"
    )


def test_lyric_text_nested_lyrics_shape():
    payload = {"Lyrics": {"Lyrics": [{"Text": "Mama,"}, {"Text": "just killed a man"}]}}
    assert jellyfin_mod._lyric_text(payload) == "Mama,\njust killed a man"


def test_lyric_text_plain_string_and_empty():
    assert jellyfin_mod._lyric_text("plain lyrics") == "plain lyrics"
    assert jellyfin_mod._lyric_text({"Lyrics": []}) is None
    assert jellyfin_mod._lyric_text({}) is None
    assert jellyfin_mod._lyric_text("") is None


def test_client_lyrics_fetches_and_joins(monkeypatch):
    requested: list[str] = []

    class _Session:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def post(self, url, *, json=None, headers=None, **k):
            return _Resp(json_body={"AccessToken": "tok", "User": {"Id": "u1"}})

        def get(self, url, *, headers=None, params=None, **k):
            requested.append(url)
            return _Resp(json_body={"Lyrics": [{"Text": "line one"}, {"Text": "two"}]})

    monkeypatch.setattr(jellyfin_mod.aiohttp, "ClientSession", _Session)
    client = RestJellyfinMusicClient("http://jf", "solaris", "pw")
    out = asyncio.run(client.lyrics("aud-1"))
    assert out == "line one\ntwo"
    assert any("/Audio/aud-1/Lyrics" in u for u in requested)


def test_client_lyrics_404_returns_none(monkeypatch):
    class _Session:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def post(self, url, *, json=None, headers=None, **k):
            return _Resp(json_body={"AccessToken": "tok", "User": {"Id": "u1"}})

        def get(self, url, *, headers=None, params=None, **k):
            return _Resp(status=404)  # track has no lyrics

    monkeypatch.setattr(jellyfin_mod.aiohttp, "ClientSession", _Session)
    client = RestJellyfinMusicClient("http://jf", "solaris", "pw")
    assert asyncio.run(client.lyrics("no-lyrics")) is None


def test_client_lyrics_5xx_degrades_to_none(monkeypatch):
    """A non-404 server error must not propagate and break the chat turn —
    lyrics is a nice-to-have, so any fetch failure returns None."""

    class _Session:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def post(self, url, *, json=None, headers=None, **k):
            return _Resp(json_body={"AccessToken": "tok", "User": {"Id": "u1"}})

        def get(self, url, *, headers=None, params=None, **k):
            return _Resp(status=503)  # Jellyfin hiccup

    monkeypatch.setattr(jellyfin_mod.aiohttp, "ClientSession", _Session)
    client = RestJellyfinMusicClient("http://jf", "solaris", "pw")
    assert asyncio.run(client.lyrics("aud-1")) is None


def test_client_lyrics_transport_error_degrades_to_none(monkeypatch):
    """A transport error / timeout during the fetch degrades to None rather
    than propagating out of the query."""

    class _Session:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def post(self, url, *, json=None, headers=None, **k):
            return _Resp(json_body={"AccessToken": "tok", "User": {"Id": "u1"}})

        def get(self, url, *, headers=None, params=None, **k):
            raise aiohttp.ClientConnectionError("connection reset")

    monkeypatch.setattr(jellyfin_mod.aiohttp, "ClientSession", _Session)
    client = RestJellyfinMusicClient("http://jf", "solaris", "pw")
    assert asyncio.run(client.lyrics("aud-1")) is None


# ---- stream_url() — castable HTTP URL for play_music (#604) ------------------


def test_stream_url_builds_castable_url_after_auth(monkeypatch):
    class _Session:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def post(self, url, *, json=None, headers=None, **k):
            return _Resp(json_body={"AccessToken": "tok123", "User": {"Id": "u-sol"}})

    monkeypatch.setattr(jellyfin_mod.aiohttp, "ClientSession", _Session)
    client = RestJellyfinMusicClient("http://jf", "solaris", "pw")
    # Default (static=True): the DIRECT/original-file form, no transcode — a Cast
    # GROUP plays it where the /universal transcode 500s (#573/#604).
    assert asyncio.run(client.stream_url("aud-1")) == (
        "http://jf/Audio/aud-1/stream?static=true&api_key=tok123"
    )
    # static=False: the /universal transcode form (the on-failure fallback), with
    # the auth in the query string (a Cast device can't send the header).
    assert asyncio.run(client.stream_url("aud-1", static=False)) == (
        "http://jf/Audio/aud-1/universal?api_key=tok123&UserId=u-sol"
        "&Container=mp3&AudioCodec=mp3&TranscodingProtocol=http"
    )


def test_stream_url_uses_cast_base_when_set(monkeypatch):
    # The Cast device fetches the URL on the LAN, so stream_url uses the
    # device-reachable cast base, not the engine's localhost base_url (#604).
    posted_to: list[str] = []

    class _Session:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def post(self, url, *, json=None, headers=None, **k):
            posted_to.append(url)
            return _Resp(json_body={"AccessToken": "tok123", "User": {"Id": "u-sol"}})

    monkeypatch.setattr(jellyfin_mod.aiohttp, "ClientSession", _Session)
    client = RestJellyfinMusicClient(
        "http://127.0.0.1:8096",
        "solaris",
        "pw",
        cast_base_url="http://192.168.178.100:8096",
    )
    # Both forms use the device-reachable cast base, not the localhost base_url.
    assert asyncio.run(client.stream_url("aud-1")) == (
        "http://192.168.178.100:8096/Audio/aud-1/stream?static=true&api_key=tok123"
    )
    assert asyncio.run(client.stream_url("aud-1", static=False)) == (
        "http://192.168.178.100:8096/Audio/aud-1/universal"
        "?api_key=tok123&UserId=u-sol"
        "&Container=mp3&AudioCodec=mp3&TranscodingProtocol=http"
    )
    # authenticate() (and so ingest/lyrics) still hit the local API base_url.
    assert posted_to == ["http://127.0.0.1:8096/Users/AuthenticateByName"]


def test_stream_url_falls_back_to_base_when_cast_unset(monkeypatch):
    class _Session:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def post(self, url, *, json=None, headers=None, **k):
            return _Resp(json_body={"AccessToken": "tok123", "User": {"Id": "u-sol"}})

    monkeypatch.setattr(jellyfin_mod.aiohttp, "ClientSession", _Session)
    # cast_base_url None (and "" via the or-fallback) ⇒ uses base_url, no regression.
    for cast in (None, ""):
        client = RestJellyfinMusicClient(
            "http://jf", "solaris", "pw", cast_base_url=cast
        )
        assert asyncio.run(client.stream_url("aud-1")) == (
            "http://jf/Audio/aud-1/stream?static=true&api_key=tok123"
        )


def test_cast_base_url_does_not_affect_api_base(monkeypatch):
    # The engine's own GET base (ingest/auth) is base_url regardless of cast base.
    paths: list[str] = []

    class _Resp2:
        def __init__(self):
            self.status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def json(self):
            return {}

        def raise_for_status(self):
            pass

    class _Session:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def get(self, url, *, params=None, headers=None, **k):
            paths.append(url)
            return _Resp2()

    client = RestJellyfinMusicClient(
        "http://127.0.0.1:8096",
        "solaris",
        "pw",
        cast_base_url="http://192.168.178.100:8096",
    )
    client._token = "tok"  # skip auth

    async def _go():
        async with _Session() as c:
            await client._get_json(c, "/Items")

    asyncio.run(_go())
    assert paths == ["http://127.0.0.1:8096/Items"]


def test_stream_url_none_without_token(monkeypatch):
    class _Session:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def post(self, url, *, json=None, headers=None, **k):
            return _Resp(json_body={})  # no AccessToken

    monkeypatch.setattr(jellyfin_mod.aiohttp, "ClientSession", _Session)
    client = RestJellyfinMusicClient("http://jf", "solaris", "pw")
    assert asyncio.run(client.stream_url("aud-1")) is None


def test_audio_uri_unchanged():
    # audio_uri stays the internal jellyfin:// scheme used by ingest (#604).
    client = RestJellyfinMusicClient("http://jf", "solaris", "pw")
    assert client.audio_uri("aud-1") == "jellyfin://audio/aud-1"

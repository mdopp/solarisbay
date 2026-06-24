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


def test_track_maps_to_song_with_metadata_and_by_edge(env):
    writer, db_path, tmp_path = env
    stats = _run(FakeJellyfinMusicClient([_track()]), writer)
    assert stats.songs_written == 1
    # The track's artist is written as a band too (so the `by` edge resolves).
    assert stats.bands_written == 1
    conn = projection.open_conn(db_path)
    song = conn.execute("SELECT * FROM entities WHERE type = 'song'").fetchone()
    assert song["canonical_name"] == "Bohemian Rhapsody"
    edge = conn.execute("SELECT * FROM facts WHERE predicate = 'by'").fetchone()
    assert edge is not None
    conn.close()
    song_text = next((tmp_path / "notes" / "okf" / "songs").glob("*.md")).read_text()
    assert "resource: jellyfin://audio/t1" in song_text
    assert "artist: Queen" in song_text
    assert "genre: Rock" in song_text
    assert "year: '1975'" in song_text or "year: 1975" in song_text


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
    # cdopp's concepts live under her private path, not the shared okf/ root.
    assert list((tmp_path / "notes" / "users" / "cdopp" / "okf" / "songs").glob("*.md"))
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
    assert (tmp_path / "notes" / "okf" / "songs").exists()
    assert (tmp_path / "notes" / "users" / "cdopp" / "okf" / "songs").exists()


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
    def __init__(self, *, json_body=None, status=200):
        self._json = json_body
        self.status = status

    async def __aenter__(self):
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
    writer, db_path, _ = env
    bad = _track(id="bad", name="王芳", artist="")
    ok = _track(id="ok", name="Good Song", artist="")
    stats = _run(FakeJellyfinMusicClient([bad, ok]), writer)
    assert stats.items == 2 and stats.skipped == 0 and stats.songs_written == 2
    conn = projection.open_conn(db_path)
    assert (
        conn.execute("SELECT COUNT(*) FROM entities WHERE type = 'song'").fetchone()[0]
        == 2
    )
    conn.close()


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

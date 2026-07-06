"""music_query tool — structured artist→songs over entities/facts (#588).

Security-critical: every query is per-owner scoped (`resident_uid IN (caller,
'household')`). The artist match is EXACT-then-prefix, never a bare substring, so
"Queen" never returns "Queens of the Stone Age". Titles are the clean
`canonical_name`, never the hash slug.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from solaris_chat.engine.tools import music_query as music_query_mod
from solaris_chat.engine.tools.music_query import build_music_query_tools
from solaris_chat.engine.tools.radio import (
    _read_default_device,
    _write_default_device,
)

# Migration 0016 subset (entities/facts/concepts) replayed locally — no alembic.
_SCHEMA = """
CREATE TABLE entities (
  id TEXT PRIMARY KEY, type TEXT NOT NULL, canonical_name TEXT NOT NULL,
  resident_uid TEXT NOT NULL, source TEXT NOT NULL, content_hash TEXT NOT NULL,
  updated TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE entity_aliases (
  entity_id TEXT NOT NULL, alias TEXT NOT NULL, PRIMARY KEY (entity_id, alias)
);
CREATE TABLE facts (
  id TEXT PRIMARY KEY, subject_entity_id TEXT, resident_uid TEXT NOT NULL,
  predicate TEXT NOT NULL, value TEXT NOT NULL, confidence REAL,
  source TEXT NOT NULL, timestamp TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE concepts (
  id TEXT PRIMARY KEY, ref_id TEXT NOT NULL, ref_kind TEXT NOT NULL,
  okf_path TEXT NOT NULL, embedding_id TEXT, content_hash TEXT NOT NULL,
  updated TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def _band(conn, ent_id, name, slug, owner, *, okf_prefix="okf", facts=()):
    conn.execute(
        "INSERT INTO entities (id, type, canonical_name, resident_uid, source,"
        " content_hash) VALUES (?, 'band', ?, ?, 'jellyfin', 'h')",
        (ent_id, name, owner),
    )
    conn.execute(
        "INSERT INTO concepts (id, ref_id, ref_kind, okf_path, content_hash)"
        " VALUES (?, ?, 'entity', ?, 'h')",
        (f"c-{ent_id}", ent_id, f"{okf_prefix}/bands/{slug}.md"),
    )
    for i, (predicate, value) in enumerate(facts):
        conn.execute(
            "INSERT INTO facts (id, subject_entity_id, resident_uid, predicate,"
            " value, source) VALUES (?, ?, ?, ?, ?, 'jellyfin')",
            (f"af-{ent_id}-{i}", ent_id, owner, predicate, value),
        )


def _song(conn, ent_id, title, band_slug, owner, *, audio_id=None):
    conn.execute(
        "INSERT INTO entities (id, type, canonical_name, resident_uid, source,"
        " content_hash) VALUES (?, 'song', ?, ?, 'jellyfin', 'h')",
        (ent_id, title, owner),
    )
    conn.execute(
        "INSERT INTO facts (id, subject_entity_id, resident_uid, predicate, value,"
        " source) VALUES (?, ?, ?, 'by', ?, 'jellyfin')",
        (f"f-{ent_id}", ent_id, owner, f"bands/{band_slug}"),
    )
    # The resource fact carries the Jellyfin audio id song_lyrics resolves to.
    conn.execute(
        "INSERT INTO facts (id, subject_entity_id, resident_uid, predicate, value,"
        " source) VALUES (?, ?, ?, 'resource', ?, 'jellyfin')",
        (f"r-{ent_id}", ent_id, owner, f"jellyfin://audio/{audio_id or ent_id}"),
    )


class _FakeLyrics:
    """A fake Jellyfin client: known audio ids return text, the rest None."""

    def __init__(self, by_id):
        self._by_id = by_id
        self.calls: list[str] = []

    async def lyrics(self, audio_id: str):
        self.calls.append(audio_id)
        return self._by_id.get(audio_id)


class _FakeJellyfin:
    """A fake Jellyfin client for play_music: stream_url builds a castable URL.

    static=True (default) is the direct/original-file form
    (`/Audio/{id}/stream?static=true`); static=False is the /universal transcode
    fallback — so a test can assert the static-first cast order (#604)."""

    def __init__(self, *, no_stream=False):
        self._no_stream = no_stream
        self.stream_calls: list[tuple[str, bool]] = []

    async def lyrics(self, audio_id: str):
        return None

    async def stream_url(self, audio_id: str, *, static: bool = True):
        self.stream_calls.append((audio_id, static))
        if self._no_stream:
            return None
        if static:
            return f"http://jf/Audio/{audio_id}/stream?static=true&api_key=tok"
        return f"http://jf/Audio/{audio_id}/universal?api_key=tok"


def _db(tmp_path) -> str:
    path = str(tmp_path / "solaris.db")
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA)
    # Two household bands whose names collide on a substring.
    _band(
        conn,
        "b-queen",
        "Queen",
        "queen",
        "household",
        facts=[("genre", "Rock"), ("bio", "British rock band formed in 1970.")],
    )
    _song(
        conn,
        "s-bohemian",
        "Bohemian Rhapsody",
        "queen",
        "household",
        audio_id="aud-boh",
    )
    _song(conn, "s-radio", "Radio Ga Ga", "queen", "household")
    _band(
        conn,
        "b-qotsa",
        "Queens of the Stone Age",
        "queens-of-the-stone-age",
        "household",
    )
    _song(conn, "s-nomone", "No One Knows", "queens-of-the-stone-age", "household")
    # Bands resolved only by FUZZY: a multi-word name (token match) and one that
    # exercises a typo edit-ratio.
    _band(
        conn,
        "b-joel",
        "Billy Joel",
        "billy-joel",
        "household",
        facts=[("genre", "Pop, Rock"), ("bio", "American pianist and singer.")],
    )
    _song(conn, "s-piano", "Piano Man", "billy-joel", "household")
    _band(conn, "b-beatles", "The Beatles", "the-beatles", "household")
    _song(conn, "s-hey", "Hey Jude", "the-beatles", "household")
    # Two bands each with a song SHARING the title "Shared Title" — to
    # disambiguate song_lyrics by artist (each has its own audio id/lyrics).
    _band(conn, "b-echo-a", "Echo Alpha", "echo-alpha", "household")
    _band(conn, "b-echo-b", "Echo Beta", "echo-beta", "household")
    _song(
        conn, "s-dup-a", "Shared Title", "echo-alpha", "household", audio_id="aud-dup-a"
    )
    _song(
        conn, "s-dup-b", "Shared Title", "echo-beta", "household", audio_id="aud-dup-b"
    )
    # A cdopp-private band+song (a private Jellyfin library, users/cdopp/okf/...).
    _band(
        conn,
        "b-private",
        "Tocotronic",
        "tocotronic",
        "cdopp",
        okf_prefix="users/cdopp/okf",
        facts=[("genre", "Indie"), ("bio", "Hamburger Band.")],
    )
    _song(conn, "s-priv", "Pure Vernunft", "tocotronic", "cdopp")
    # cdopp-private band ALSO named "Queen", whose song shares the SAME
    # `bands/queen` by-edge value as the household Queen — the by-edge alone
    # would leak it cross-owner; only the resident_uid scope keeps it private.
    _band(
        conn,
        "b-queen-cdopp",
        "Queen",
        "queen",
        "cdopp",
        okf_prefix="users/cdopp/okf",
    )
    _song(conn, "s-queen-cdopp", "Secret Queen Track", "queen", "cdopp")
    conn.commit()
    conn.close()
    return path


def _tool(db, uid):
    (t,) = build_music_query_tools(db, lambda: uid)
    return t


async def _call(db, uid, args):
    return json.loads(await _tool(db, uid).handler(args))


async def _call_lyrics(db, uid, args, client):
    tools = build_music_query_tools(db, lambda: uid, client)
    (t,) = [t for t in tools if t.name == "music_query"]
    return json.loads(await t.handler(args))


# ---- exact resolve: Queen != Queens of the Stone Age -------------------------


async def test_songs_by_artist_exact_not_substring(tmp_path):
    db = _db(tmp_path)
    out = await _call(db, "mdopp", {"op": "songs_by_artist", "artist": "Queen"})
    assert out["artist"] == "Queen"
    assert out["total"] == 2
    assert set(out["songs"]) == {"Bohemian Rhapsody", "Radio Ga Ga"}
    # The QOTSA track must NOT leak into a "Queen" query.
    assert "No One Knows" not in out["songs"]


async def test_songs_returns_clean_titles_not_slugs(tmp_path):
    db = _db(tmp_path)
    out = await _call(db, "mdopp", {"op": "songs_by_artist", "artist": "Queen"})
    assert all("-" not in s or " " in s for s in out["songs"])  # no hash slugs
    assert "Bohemian Rhapsody" in out["songs"]


async def test_songs_by_artist_qotsa_isolated(tmp_path):
    db = _db(tmp_path)
    out = await _call(
        db, "mdopp", {"op": "songs_by_artist", "artist": "Queens of the Stone Age"}
    )
    assert out["songs"] == ["No One Knows"]


async def test_songs_by_artist_unknown(tmp_path):
    db = _db(tmp_path)
    out = await _call(db, "mdopp", {"op": "songs_by_artist", "artist": "Nirvana"})
    assert out == {"artist": "Nirvana", "total": 0, "songs": []}


# ---- ranked fuzzy resolve (only when NO exact match) -------------------------


async def test_exact_lowercase_wins_over_fuzzy(tmp_path):
    db = _db(tmp_path)
    # 'queen' (lowercase) is an EXACT case-insensitive band — must resolve to
    # Queen, never fuzz to Queens of the Stone Age.
    out = await _call(db, "mdopp", {"op": "songs_by_artist", "artist": "queen"})
    assert out["artist"] == "Queen"
    assert set(out["songs"]) == {"Bohemian Rhapsody", "Radio Ga Ga"}


async def test_fuzzy_token_resolves_billy_joel(tmp_path):
    db = _db(tmp_path)
    # 'Joel' is a whole word in 'Billy Joel' — no exact match, so fuzzy finds it.
    out = await _call(db, "mdopp", {"op": "songs_by_artist", "artist": "Joel"})
    assert out["artist"] == "Billy Joel"
    assert out["songs"] == ["Piano Man"]


async def test_fuzzy_full_name_resolves_billy_joel(tmp_path):
    db = _db(tmp_path)
    out = await _call(db, "mdopp", {"op": "songs_by_artist", "artist": "billy joel"})
    assert out["artist"] == "Billy Joel"
    assert out["songs"] == ["Piano Man"]


async def test_fuzzy_does_not_override_exact_queen(tmp_path):
    db = _db(tmp_path)
    # 'Queen' is exact -> Queen, NEVER fuzzed to QOTSA even though QOTSA contains
    # the word 'Queens'.
    out = await _call(db, "mdopp", {"op": "songs_by_artist", "artist": "Queen"})
    assert out["artist"] == "Queen"
    assert "No One Knows" not in out["songs"]


async def test_fuzzy_queens_resolves_qotsa(tmp_path):
    db = _db(tmp_path)
    # 'Queens' (plural) has no exact band; whole-word containment keeps it on
    # QOTSA, not a typo-near 'Queen'.
    out = await _call(db, "mdopp", {"op": "songs_by_artist", "artist": "Queens"})
    assert out["artist"] == "Queens of the Stone Age"
    assert out["songs"] == ["No One Knows"]


async def test_fuzzy_typo_resolves_beatles(tmp_path):
    db = _db(tmp_path)
    # 'Beatls' is a typo of the 'Beatles' word in 'The Beatles' — edit-ratio.
    out = await _call(db, "mdopp", {"op": "songs_by_artist", "artist": "Beatls"})
    assert out["artist"] == "The Beatles"
    assert out["songs"] == ["Hey Jude"]


async def test_fuzzy_nonsense_returns_not_found(tmp_path):
    db = _db(tmp_path)
    # Nothing clears the threshold -> not-found, never a random band.
    out = await _call(
        db, "mdopp", {"op": "songs_by_artist", "artist": "xqzptv nonsense"}
    )
    assert out["total"] == 0
    assert out["songs"] == []


async def test_fuzzy_does_not_leak_private_band(tmp_path):
    db = _db(tmp_path)
    # A fuzzy query for the cdopp-private 'Tocotronic' must NOT surface it for
    # mdopp or household (scoping holds on the fuzzy path too)...
    for uid in ("mdopp", "household"):
        out = await _call(db, uid, {"op": "songs_by_artist", "artist": "Tocotron"})
        assert out["total"] == 0
        assert out["songs"] == []
    # ...but the owner DOES get a fuzzy match on their own private band.
    out = await _call(db, "cdopp", {"op": "songs_by_artist", "artist": "Tocotron"})
    assert out["artist"] == "Tocotronic"
    assert out["songs"] == ["Pure Vernunft"]


# ---- list_artists ------------------------------------------------------------


async def test_list_artists_returns_both(tmp_path):
    db = _db(tmp_path)
    out = await _call(db, "mdopp", {"op": "list_artists"})
    assert "Queen" in out["artists"]
    assert "Queens of the Stone Age" in out["artists"]


async def test_list_artists_prefix(tmp_path):
    db = _db(tmp_path)
    out = await _call(db, "mdopp", {"op": "list_artists", "prefix": "Queen"})
    # Prefix matches both Queen and Queens of the Stone Age (both start "Queen").
    assert set(out["artists"]) == {"Queen", "Queens of the Stone Age"}
    out = await _call(db, "mdopp", {"op": "list_artists", "prefix": "Toco"})
    # Toco* is cdopp-private -> not visible to mdopp.
    assert out["artists"] == []


# ---- per-user scoping (security-critical) ------------------------------------


async def test_private_band_withheld_from_other_resident(tmp_path):
    db = _db(tmp_path)
    # cdopp's private "Tocotronic" must not surface for mdopp or for household.
    for uid in ("mdopp", "household"):
        out = await _call(db, uid, {"op": "list_artists"})
        assert "Tocotronic" not in out["artists"]
        songs = await _call(db, uid, {"op": "songs_by_artist", "artist": "Tocotronic"})
        assert songs["total"] == 0


async def test_private_band_visible_to_owner(tmp_path):
    db = _db(tmp_path)
    out = await _call(db, "cdopp", {"op": "list_artists"})
    assert "Tocotronic" in out["artists"]
    songs = await _call(db, "cdopp", {"op": "songs_by_artist", "artist": "Tocotronic"})
    assert songs["songs"] == ["Pure Vernunft"]
    # cdopp still sees the shared household library too.
    assert "Queen" in out["artists"]


async def test_unknown_caller_sees_household_only(tmp_path):
    db = _db(tmp_path)
    out = await _call(db, "", {"op": "list_artists"})  # unknown -> household
    assert "Queen" in out["artists"]
    assert "Tocotronic" not in out["artists"]


async def test_songs_by_value_no_cross_owner_collision(tmp_path):
    db = _db(tmp_path)
    # household/mdopp ask for "Queen": the by-edge value `bands/queen` is shared
    # with a cdopp-private "Queen", but the resident_uid scope on the song must
    # withhold the private track.
    for uid in ("mdopp", "household"):
        out = await _call(db, uid, {"op": "songs_by_artist", "artist": "Queen"})
        assert set(out["songs"]) == {"Bohemian Rhapsody", "Radio Ga Ga"}
        assert "Secret Queen Track" not in out["songs"]
    # cdopp resolves to its OWN private Queen and sees its private track.
    out = await _call(db, "cdopp", {"op": "songs_by_artist", "artist": "Queen"})
    assert "Secret Queen Track" in out["songs"]


async def test_wildcard_arg_not_substring(tmp_path):
    db = _db(tmp_path)
    # A bare "%" must NOT expand into a wildcard that matches every band.
    out = await _call(db, "mdopp", {"op": "songs_by_artist", "artist": "%"})
    assert out["total"] == 0
    out = await _call(db, "mdopp", {"op": "list_artists", "prefix": "%"})
    assert out["artists"] == []
    # "Q%" stays a literal prefix — there is no band literally named "Q%...".
    out = await _call(db, "mdopp", {"op": "list_artists", "prefix": "Q%"})
    assert out["artists"] == []
    out = await _call(db, "mdopp", {"op": "songs_by_artist", "artist": "Q%"})
    assert out["total"] == 0
    # "%eens" must not wildcard-match Queens of the Stone Age.
    out = await _call(db, "mdopp", {"op": "songs_by_artist", "artist": "%eens"})
    assert out["total"] == 0


# ---- artist_info (#592): genre + bio + song_count ----------------------------


async def test_artist_info_returns_facts_and_song_count(tmp_path):
    db = _db(tmp_path)
    out = await _call(db, "mdopp", {"op": "artist_info", "artist": "Queen"})
    assert out["artist"] == "Queen"
    assert out["genre"] == "Rock"
    assert out["bio"] == "British rock band formed in 1970."
    assert out["song_count"] == 2
    # Clean output — no hash ids/slugs.
    assert "id" not in out and "slug" not in out


async def test_artist_info_fuzzy_resolves_billy_joel(tmp_path):
    db = _db(tmp_path)
    # 'Joel' (no exact band) reuses the same fuzzy resolver as songs_by_artist.
    out = await _call(db, "mdopp", {"op": "artist_info", "artist": "Joel"})
    assert out["artist"] == "Billy Joel"
    assert out["genre"] == "Pop, Rock"
    assert out["bio"] == "American pianist and singer."
    assert out["song_count"] == 1


async def test_artist_info_unknown_not_found(tmp_path):
    db = _db(tmp_path)
    out = await _call(db, "mdopp", {"op": "artist_info", "artist": "Nirvana"})
    assert out == {"artist": "Nirvana", "found": False}


async def test_artist_info_private_facts_withheld_from_others(tmp_path):
    db = _db(tmp_path)
    # cdopp's private band's facts must NOT surface for mdopp or household...
    for uid in ("mdopp", "household"):
        out = await _call(db, uid, {"op": "artist_info", "artist": "Tocotronic"})
        assert out == {"artist": "Tocotronic", "found": False}
    # ...but the owner DOES get its genre/bio + song_count.
    out = await _call(db, "cdopp", {"op": "artist_info", "artist": "Tocotronic"})
    assert out["artist"] == "Tocotronic"
    assert out["genre"] == "Indie"
    assert out["bio"] == "Hamburger Band."
    assert out["song_count"] == 1


async def test_artist_info_description_steers_was_weiss_ich(tmp_path):
    db = _db(tmp_path)
    desc = _tool(db, "mdopp").description
    assert "artist_info" in desc
    assert "Genre" in desc and "Kurzbio" in desc


async def test_bad_op(tmp_path):
    db = _db(tmp_path)
    out = await _call(db, "mdopp", {"op": "nonsense"})
    assert "error" in out


# ---- song_lyrics (#593): on-demand live lyrics ------------------------------


async def test_song_lyrics_returns_lyrics(tmp_path):
    db = _db(tmp_path)
    client = _FakeLyrics({"aud-boh": "Is this the real life?\nIs this just fantasy?"})
    out = await _call_lyrics(
        db, "mdopp", {"op": "song_lyrics", "title": "Bohemian Rhapsody"}, client
    )
    assert out["title"] == "Bohemian Rhapsody"
    assert out["artist"] == "Queen"
    assert out["lyrics"].startswith("Is this the real life?")
    assert client.calls == ["aud-boh"]


async def test_song_lyrics_fuzzy_title_resolves(tmp_path):
    db = _db(tmp_path)
    client = _FakeLyrics({"aud-boh": "Mama, just killed a man"})
    # A near-miss title fuzzy-resolves to Bohemian Rhapsody.
    out = await _call_lyrics(
        db, "mdopp", {"op": "song_lyrics", "title": "Bohemian Rapsody"}, client
    )
    assert out["title"] == "Bohemian Rhapsody"
    assert out["lyrics"] == "Mama, just killed a man"


async def test_song_lyrics_artist_disambiguates_duplicate_title(tmp_path):
    db = _db(tmp_path)
    client = _FakeLyrics({"aud-dup-a": "alpha words", "aud-dup-b": "beta words"})
    # Two "Shared Title" songs; artist='Echo Beta' must pick the Beta one.
    out = await _call_lyrics(
        db,
        "mdopp",
        {"op": "song_lyrics", "title": "Shared Title", "artist": "Echo Beta"},
        client,
    )
    assert out["title"] == "Shared Title"
    assert out["artist"] == "Echo Beta"
    assert out["lyrics"] == "beta words"
    assert client.calls == ["aud-dup-b"]


async def test_song_lyrics_no_lyrics_graceful(tmp_path):
    db = _db(tmp_path)
    # The known song has no lyrics on the server (client returns None).
    client = _FakeLyrics({})
    out = await _call_lyrics(
        db, "mdopp", {"op": "song_lyrics", "title": "Bohemian Rhapsody"}, client
    )
    assert out["title"] == "Bohemian Rhapsody"
    assert out["lyrics"] is None
    assert out["note"] == "keine Lyrics verfügbar"


async def test_song_lyrics_song_not_found(tmp_path):
    db = _db(tmp_path)
    client = _FakeLyrics({"aud-boh": "x"})
    out = await _call_lyrics(
        db, "mdopp", {"op": "song_lyrics", "title": "Xqzptv Nonsense"}, client
    )
    assert out == {"found": False}
    assert client.calls == []  # no song resolved -> no live fetch


async def test_song_lyrics_per_user_scoping(tmp_path):
    db = _db(tmp_path)
    # cdopp's private "Pure Vernunft" must not be fetchable for mdopp/household.
    client = _FakeLyrics({"s-priv": "Privater Text"})
    for uid in ("mdopp", "household"):
        out = await _call_lyrics(
            db, uid, {"op": "song_lyrics", "title": "Pure Vernunft"}, client
        )
        assert out == {"found": False}
    assert client.calls == []
    # The owner DOES get her private song's lyrics.
    out = await _call_lyrics(
        db, "cdopp", {"op": "song_lyrics", "title": "Pure Vernunft"}, client
    )
    assert out["title"] == "Pure Vernunft"
    assert out["lyrics"] == "Privater Text"


async def test_song_lyrics_degrades_without_client(tmp_path):
    db = _db(tmp_path)
    # Jellyfin unconfigured (no client): the song resolves but lyrics degrade.
    out = await _call(db, "mdopp", {"op": "song_lyrics", "title": "Bohemian Rhapsody"})
    assert out["title"] == "Bohemian Rhapsody"
    assert out["lyrics"] is None
    assert out["note"] == "keine Lyrics verfügbar"


async def test_song_lyrics_description_steers(tmp_path):
    db = _db(tmp_path)
    desc = _tool(db, "mdopp").description
    assert "song_lyrics" in desc
    assert "Liedtext" in desc


# ---- play_music (#604): cast a library track on a media_player --------------


def _play_tool(db, uid, client, *, notes_dir=""):
    """The play_music tool (registered only with a client + HA creds)."""
    tools = build_music_query_tools(
        db,
        lambda: uid,
        client,
        hass_url="http://ha",
        hass_token="tok",
        notes_dir=notes_dir,
    )
    (play,) = [t for t in tools if t.name == "play_music"]
    return play


def _stub_play(monkeypatch, *, ok=True):
    """Record media_player.play_media calls in place of call_service_scoped."""
    calls: list[tuple] = []

    async def _fake(hass_url, hass_token, entity_id, service, data):
        calls.append((hass_url, hass_token, entity_id, service, data))
        return {"ok": ok, "state": "playing" if ok else None}

    monkeypatch.setattr(music_query_mod, "call_service_scoped", _fake)
    monkeypatch.setattr(music_query_mod.asyncio, "sleep", _noop_sleep)
    return calls


async def _noop_sleep(_seconds):
    return None


async def _call_play(db, uid, args, client, monkeypatch, *, ok=True, notes_dir=""):
    calls = _stub_play(monkeypatch, ok=ok)
    out = json.loads(
        await _play_tool(db, uid, client, notes_dir=notes_dir).handler(args)
    )
    return out, calls


async def test_play_music_registered_only_with_client_and_creds(tmp_path):
    db = _db(tmp_path)
    # No HA creds -> no play_music (needs a cast target); playlist_add still
    # registers on a live client alone (no HA cast involved).
    names = {
        t.name for t in build_music_query_tools(db, lambda: "mdopp", _FakeJellyfin())
    }
    assert names == {"music_query", "playlist_add"}
    # Client + creds -> play_music registers.
    names = {
        t.name
        for t in build_music_query_tools(
            db, lambda: "mdopp", _FakeJellyfin(), hass_url="http://ha", hass_token="t"
        )
    }
    assert "play_music" in names


async def test_play_music_title_casts_stream_url(tmp_path, monkeypatch):
    db = _db(tmp_path)
    out, calls = await _call_play(
        db,
        "mdopp",
        {"title": "Bohemian Rhapsody", "entity_id": "media_player.kuche"},
        _FakeJellyfin(),
        monkeypatch,
    )
    assert out == {
        "ok": True,
        "title": "Bohemian Rhapsody",
        "artist": "",
        "entity_id": "media_player.kuche",
        "played": True,
        "audio_id": "aud-boh",
    }
    # The HA call is media_player.play_media with content_type=music + the stream URL.
    (_, _, entity_id, service, data) = calls[0]
    assert service == "media_player.play_media"
    assert entity_id == "media_player.kuche"
    assert data["media_content_type"] == "music"
    # Static (direct/original-file) URL is cast FIRST (group-friendly, #604).
    assert (
        data["media_content_id"]
        == "http://jf/Audio/aud-boh/stream?static=true&api_key=tok"
    )


async def test_play_music_artist_plays_first_real_track(tmp_path, monkeypatch):
    db = _db(tmp_path)
    out, calls = await _call_play(
        db,
        "mdopp",
        {"artist": "Queen", "entity_id": "media_player.kuche"},
        _FakeJellyfin(),
        monkeypatch,
    )
    assert out["ok"] is True and out["played"] is True
    # Bohemian Rhapsody sorts first AND has an audio id; Radio Ga Ga lacks one.
    assert out["title"] == "Bohemian Rhapsody"
    assert calls[0][4]["media_content_id"].endswith(
        "/aud-boh/stream?static=true&api_key=tok"
    )


async def test_play_music_missing_device_no_ha_call(tmp_path, monkeypatch):
    db = _db(tmp_path)
    out, calls = await _call_play(
        db, "mdopp", {"title": "Bohemian Rhapsody"}, _FakeJellyfin(), monkeypatch
    )
    assert out == {
        "ok": False,
        "reason": "need_default_device",
        "say": "Auf welchem Gerät soll ich standardmäßig spielen?",
    }
    assert calls == []


async def test_play_music_no_match_no_cast(tmp_path, monkeypatch):
    db = _db(tmp_path)
    # An unresolvable title NEVER falls back to another track/podcast (#604).
    out, calls = await _call_play(
        db,
        "mdopp",
        {"title": "Xqzptv Nonsense", "entity_id": "media_player.kuche"},
        _FakeJellyfin(),
        monkeypatch,
    )
    assert out == {"ok": False, "reason": "not_found", "query": "Xqzptv Nonsense"}
    assert calls == []  # anti-podcast-fallback: no play_media POST


async def test_play_music_artist_not_found_no_cast(tmp_path, monkeypatch):
    db = _db(tmp_path)
    out, calls = await _call_play(
        db,
        "mdopp",
        {"artist": "Nirvana", "entity_id": "media_player.kuche"},
        _FakeJellyfin(),
        monkeypatch,
    )
    assert out == {"ok": False, "reason": "artist_not_found"}
    assert calls == []


async def test_play_music_per_user_scoping(tmp_path, monkeypatch):
    db = _db(tmp_path)
    # cdopp's private "Secret Queen Track" must not be playable for mdopp/household.
    for uid in ("mdopp", "household"):
        out, calls = await _call_play(
            db,
            uid,
            {"title": "Secret Queen Track", "entity_id": "media_player.kuche"},
            _FakeJellyfin(),
            monkeypatch,
        )
        assert out["ok"] is False and out["reason"] == "not_found"
        assert calls == []
    # The owner CAN play her own private track.
    out, calls = await _call_play(
        db,
        "cdopp",
        {"title": "Secret Queen Track", "entity_id": "media_player.kuche"},
        _FakeJellyfin(),
        monkeypatch,
    )
    assert out["ok"] is True and out["title"] == "Secret Queen Track"
    assert len(calls) == 1


# -- u99: device-less play defaults to the originating room's media_player ----


def _play_tool_with_room(db, uid, client, *, room, resolver, notes_dir=""):
    tools = build_music_query_tools(
        db,
        lambda: uid,
        client,
        hass_url="http://ha",
        hass_token="tok",
        room_getter=lambda: room,
        room_resolver=resolver,
        notes_dir=notes_dir,
    )
    (play,) = [t for t in tools if t.name == "play_music"]
    return play


async def test_play_music_defaults_to_current_room(tmp_path, monkeypatch):
    db = _db(tmp_path)
    calls = _stub_play(monkeypatch)

    async def _resolver(room):
        return "media_player.kuche" if room == "Küche" else None

    play = _play_tool_with_room(
        db, "mdopp", _FakeJellyfin(), room="Küche", resolver=_resolver
    )
    out = json.loads(await play.handler({"title": "Bohemian Rhapsody"}))
    # No entity_id named, but a current room is known → cast there.
    assert out["ok"] is True
    assert out["entity_id"] == "media_player.kuche"
    assert calls[0][2] == "media_player.kuche"


async def test_play_music_named_device_wins_over_room(tmp_path, monkeypatch):
    db = _db(tmp_path)
    calls = _stub_play(monkeypatch)

    async def _resolver(room):
        return "media_player.kuche"

    play = _play_tool_with_room(
        db, "mdopp", _FakeJellyfin(), room="Küche", resolver=_resolver
    )
    out = json.loads(
        await play.handler(
            {"title": "Bohemian Rhapsody", "entity_id": "media_player.bad"}
        )
    )
    assert out["entity_id"] == "media_player.bad"
    assert calls[0][2] == "media_player.bad"


async def test_play_music_no_room_no_device_need_default_device(tmp_path, monkeypatch):
    db = _db(tmp_path)
    calls = _stub_play(monkeypatch)

    async def _resolver(room):
        return None

    # No entity_id AND no current room AND no stored default → need_default_device.
    play = _play_tool_with_room(
        db,
        "mdopp",
        _FakeJellyfin(),
        room="",
        resolver=_resolver,
        notes_dir=str(tmp_path),
    )
    out = json.loads(await play.handler({"title": "Bohemian Rhapsody"}))
    assert out == {
        "ok": False,
        "reason": "need_default_device",
        "say": "Auf welchem Gerät soll ich standardmäßig spielen?",
    }
    assert calls == []


# -- u103 (#622): learned per-user default playback device -------------------


async def test_play_music_explicit_device_stores_default(tmp_path, monkeypatch):
    db = _db(tmp_path)
    notes = str(tmp_path)
    # First explicit device with no default yet -> casts there AND stores it.
    out, calls = await _call_play(
        db,
        "mdopp",
        {"title": "Bohemian Rhapsody", "entity_id": "media_player.kuche"},
        _FakeJellyfin(),
        monkeypatch,
        notes_dir=notes,
    )
    assert out["ok"] is True and out["entity_id"] == "media_player.kuche"
    assert calls[0][2] == "media_player.kuche"
    assert _read_default_device(notes, "mdopp") == "media_player.kuche"


async def test_play_music_deviceless_reuses_stored_default(tmp_path, monkeypatch):
    db = _db(tmp_path)
    notes = str(tmp_path)
    _write_default_device(notes, "mdopp", "media_player.bad")
    # No device, no room, but a stored default -> cast there (no need_default_device).
    out, calls = await _call_play(
        db,
        "mdopp",
        {"title": "Bohemian Rhapsody"},
        _FakeJellyfin(),
        monkeypatch,
        notes_dir=notes,
    )
    assert out["ok"] is True and out["entity_id"] == "media_player.bad"
    assert calls[0][2] == "media_player.bad"


async def test_play_music_explicit_oneoff_keeps_stored_default(tmp_path, monkeypatch):
    db = _db(tmp_path)
    notes = str(tmp_path)
    _write_default_device(notes, "mdopp", "media_player.bad")
    # An explicit device when a default already exists is a one-off; default stays.
    out, calls = await _call_play(
        db,
        "mdopp",
        {"title": "Bohemian Rhapsody", "entity_id": "media_player.kuche"},
        _FakeJellyfin(),
        monkeypatch,
        notes_dir=notes,
    )
    assert out["entity_id"] == "media_player.kuche"
    assert calls[0][2] == "media_player.kuche"
    assert _read_default_device(notes, "mdopp") == "media_player.bad"


async def test_play_music_room_wins_over_stored_default(tmp_path, monkeypatch):
    db = _db(tmp_path)
    notes = str(tmp_path)
    _write_default_device(notes, "mdopp", "media_player.bad")
    calls = _stub_play(monkeypatch)

    async def _resolver(room):
        return "media_player.kuche" if room == "Küche" else None

    play = _play_tool_with_room(
        db, "mdopp", _FakeJellyfin(), room="Küche", resolver=_resolver, notes_dir=notes
    )
    out = json.loads(await play.handler({"title": "Bohemian Rhapsody"}))
    # Current room takes precedence over the stored default.
    assert out["entity_id"] == "media_player.kuche"
    assert calls[0][2] == "media_player.kuche"
    assert _read_default_device(notes, "mdopp") == "media_player.bad"


async def test_play_music_default_device_is_per_user(tmp_path, monkeypatch):
    db = _db(tmp_path)
    notes = str(tmp_path)
    _write_default_device(notes, "alice", "media_player.alice")
    # Caller B has no default of their own and must NOT read A's.
    out, calls = await _call_play(
        db,
        "bob",
        {"title": "Bohemian Rhapsody"},
        _FakeJellyfin(),
        monkeypatch,
        notes_dir=notes,
    )
    assert out == {
        "ok": False,
        "reason": "need_default_device",
        "say": "Auf welchem Gerät soll ich standardmäßig spielen?",
    }
    assert calls == []
    assert _read_default_device(notes, "bob") is None
    fav = Path(notes) / "users" / "alice" / "preferences" / "default-device.md"
    assert fav.is_file()  # A's note untouched


async def test_play_music_no_stream_when_url_none(tmp_path, monkeypatch):
    db = _db(tmp_path)
    out, calls = await _call_play(
        db,
        "mdopp",
        {"title": "Bohemian Rhapsody", "entity_id": "media_player.kuche"},
        _FakeJellyfin(no_stream=True),
        monkeypatch,
    )
    assert out == {"ok": False, "reason": "no_stream"}
    assert calls == []


async def test_play_music_play_failed_never_played(tmp_path, monkeypatch):
    db = _db(tmp_path)
    out, calls = await _call_play(
        db,
        "mdopp",
        {"title": "Bohemian Rhapsody", "entity_id": "media_player.kuche"},
        _FakeJellyfin(),
        monkeypatch,
        ok=False,
    )
    assert out["ok"] is False and out["reason"] == "play_failed"
    assert out["title"] == "Bohemian Rhapsody"
    assert "played" not in out  # truthful: never claim played on a failed cast
    # Both URL forms (static then universal) each exhaust their bounded retry.
    assert len(calls) == 2 * (music_query_mod._PLAY_RETRIES + 1)


# ---- u92 (#604): filler-title parse, random play, error-detail + retry -------


def _seq_play(monkeypatch, results):
    """call_service_scoped returning a queued sequence of dicts, recording calls."""
    calls: list[tuple] = []
    seq = list(results)

    async def _fake(hass_url, hass_token, entity_id, service, data):
        calls.append((hass_url, hass_token, entity_id, service, data))
        return seq[min(len(calls) - 1, len(seq) - 1)]

    monkeypatch.setattr(music_query_mod, "call_service_scoped", _fake)
    # No real backoff sleeps in tests.
    monkeypatch.setattr(music_query_mod.asyncio, "sleep", _noop_sleep)
    return calls


async def test_play_music_filler_title_strips_to_artist(tmp_path, monkeypatch):
    db = _db(tmp_path)
    # The model stuffed "ein Song von Queen" into title (no artist). It must be
    # parsed as artist=Queen, title empty -> a REAL Queen track, never the filler.
    out, calls = await _call_play(
        db,
        "mdopp",
        {"title": "ein Song von Queen", "entity_id": "media_player.kuche"},
        _FakeJellyfin(),
        monkeypatch,
    )
    assert out["ok"] is True and out["played"] is True
    assert out["title"] == "Bohemian Rhapsody"  # never the filler phrase
    assert calls[0][4]["media_content_id"].endswith(
        "/aud-boh/stream?static=true&api_key=tok"
    )


async def test_play_music_filler_musik_von(tmp_path, monkeypatch):
    db = _db(tmp_path)
    out, calls = await _call_play(
        db,
        "mdopp",
        {"title": "Musik von Queen", "entity_id": "media_player.kuche"},
        _FakeJellyfin(),
        monkeypatch,
    )
    assert out["ok"] is True
    assert out["title"] == "Bohemian Rhapsody"


async def test_play_music_random_when_empty(tmp_path, monkeypatch):
    db = _db(tmp_path)
    # Both empty -> a random castable track (one with an audio id) plays.
    out, calls = await _call_play(
        db,
        "mdopp",
        {"entity_id": "media_player.kuche"},
        _FakeJellyfin(),
        monkeypatch,
    )
    assert out["ok"] is True and out["played"] is True
    assert out["title"]  # a real title, reported
    assert len(calls) == 1
    assert "/stream?static=true&api_key=tok" in calls[0][4]["media_content_id"]


async def test_play_music_random_scoped_no_private_leak(tmp_path, monkeypatch):
    db = _db(tmp_path)
    # A household caller's random pick must NEVER be a cdopp-private track.
    private_titles = {"Pure Vernunft", "Secret Queen Track"}
    for _ in range(40):  # random.choice -> sample repeatedly
        out, _ = await _call_play(
            db,
            "household",
            {"entity_id": "media_player.kuche"},
            _FakeJellyfin(),
            monkeypatch,
        )
        assert out["ok"] is True
        assert out["title"] not in private_titles


async def test_play_music_artist_fallback_on_unresolved_title(tmp_path, monkeypatch):
    db = _db(tmp_path)
    # A title that does NOT resolve but an artist is present -> fall back to the
    # artist's first castable track (never echo the unresolved title).
    out, calls = await _call_play(
        db,
        "mdopp",
        {
            "title": "Xqzptv Nonsense",
            "artist": "Queen",
            "entity_id": "media_player.kuche",
        },
        _FakeJellyfin(),
        monkeypatch,
    )
    assert out["ok"] is True
    assert out["title"] == "Bohemian Rhapsody"  # not the unresolved title


async def test_play_music_play_failed_surfaces_detail(tmp_path, monkeypatch):
    db = _db(tmp_path)
    calls = _seq_play(monkeypatch, [{"ok": False, "error": "HA 500: boom"}])
    out = json.loads(
        await _play_tool(db, "mdopp", _FakeJellyfin()).handler(
            {"title": "Bohemian Rhapsody", "entity_id": "media_player.kuche"}
        )
    )
    assert out["ok"] is False and out["reason"] == "play_failed"
    assert out["detail"] == "HA 500: boom"  # the HA error is surfaced, not swallowed
    # Both forms retried: static (group-friendly) then universal (transcode).
    assert len(calls) == 2 * (music_query_mod._PLAY_RETRIES + 1)


async def test_play_music_retry_then_success(tmp_path, monkeypatch):
    db = _db(tmp_path)
    # Fail once, then succeed -> ok:true after the retry (Cast flakiness #573).
    calls = _seq_play(
        monkeypatch,
        [{"ok": False, "error": "HA 500: flaky"}, {"ok": True, "state": "playing"}],
    )
    out = json.loads(
        await _play_tool(db, "mdopp", _FakeJellyfin()).handler(
            {"title": "Bohemian Rhapsody", "entity_id": "media_player.kuche"}
        )
    )
    assert out["ok"] is True and out["played"] is True
    assert len(calls) == 2  # one fail + one retry that succeeded


async def test_play_music_static_fails_then_universal_succeeds(tmp_path, monkeypatch):
    db = _db(tmp_path)
    # The Cast GROUP 500s on the static/direct URL for a non-Cast-native container,
    # so play_music falls back to the /universal transcode form, which plays (#604).
    calls: list[tuple] = []

    async def _fake(hass_url, hass_token, entity_id, service, data):
        calls.append((hass_url, hass_token, entity_id, service, data))
        url = data["media_content_id"]
        if "static=true" in url:
            return {"ok": False, "error": "HA 500: group static reject"}
        return {"ok": True, "state": "playing"}

    monkeypatch.setattr(music_query_mod, "call_service_scoped", _fake)
    monkeypatch.setattr(music_query_mod.asyncio, "sleep", _noop_sleep)
    out = json.loads(
        await _play_tool(db, "mdopp", _FakeJellyfin()).handler(
            {"title": "Bohemian Rhapsody", "entity_id": "media_player.wohnzimmer"}
        )
    )
    assert out["ok"] is True and out["played"] is True
    # Static was tried first (its retries exhausted), then universal cast + won.
    static_calls = [c for c in calls if "static=true" in c[4]["media_content_id"]]
    universal_calls = [c for c in calls if "/universal?" in c[4]["media_content_id"]]
    assert static_calls and universal_calls
    assert universal_calls[-1] is calls[-1]  # universal is the winning final cast


async def test_play_music_both_forms_fail_surfaces_detail(tmp_path, monkeypatch):
    db = _db(tmp_path)
    # Both static AND universal 500 -> ok:false with the last HA error surfaced.
    calls = _seq_play(monkeypatch, [{"ok": False, "error": "HA 500: both"}])
    out = json.loads(
        await _play_tool(db, "mdopp", _FakeJellyfin()).handler(
            {"title": "Bohemian Rhapsody", "entity_id": "media_player.wohnzimmer"}
        )
    )
    assert out["ok"] is False and out["reason"] == "play_failed"
    assert out["detail"] == "HA 500: both"
    assert len(calls) == 2 * (music_query_mod._PLAY_RETRIES + 1)


async def test_play_music_need_device_when_no_entity(tmp_path, monkeypatch):
    db = _db(tmp_path)
    out, calls = await _call_play(
        db, "mdopp", {"artist": "Queen"}, _FakeJellyfin(), monkeypatch
    )
    assert out == {
        "ok": False,
        "reason": "need_default_device",
        "say": "Auf welchem Gerät soll ich standardmäßig spielen?",
    }
    assert calls == []


# -- group-cast fallback (#638): a 500 on a Cast group → a same-area device ----


def _play_tool_fallback(db, uid, client, fallbacks):
    async def _area_fallback(entity_id):
        return list(fallbacks)

    tools = build_music_query_tools(
        db,
        lambda: uid,
        client,
        hass_url="http://ha",
        hass_token="tok",
        area_fallback=_area_fallback,
    )
    (play,) = [t for t in tools if t.name == "play_music"]
    return play


def _seq_play_by_entity(monkeypatch, fn):
    """call_service_scoped delegating to fn(entity_id, url), recording calls."""
    calls: list[tuple] = []

    async def _fake(hass_url, hass_token, entity_id, service, data):
        calls.append((entity_id, data["media_content_id"]))
        return fn(entity_id, data["media_content_id"])

    monkeypatch.setattr(music_query_mod, "call_service_scoped", _fake)
    monkeypatch.setattr(music_query_mod.asyncio, "sleep", _noop_sleep)
    return calls


async def test_play_music_group_500_falls_back_to_voice_pe(tmp_path, monkeypatch):
    db = _db(tmp_path)
    voice = "media_player.home_assistant_voice_0907c9_media_player"

    # The Cast group 500s on every form; the same-area Voice PE plays.
    def _fake(entity_id, _url):
        if entity_id == "media_player.wohnzimmer":
            return {"ok": False, "error": "HA 500: group reject"}
        return {"ok": True, "state": "playing"}

    calls = _seq_play_by_entity(monkeypatch, _fake)
    play = _play_tool_fallback(
        db, "mdopp", _FakeJellyfin(), [voice, "media_player.wohnzimmer_sonos"]
    )
    out = json.loads(
        await play.handler(
            {"title": "Bohemian Rhapsody", "entity_id": "media_player.wohnzimmer"}
        )
    )
    assert out["ok"] is True and out["played"] is True
    # Reports the Voice PE that actually played (preferred candidate), not the group.
    assert out["entity_id"] == voice
    assert calls[0][0] == "media_player.wohnzimmer"  # the group tried first
    assert any(c[0] == voice for c in calls)


async def test_play_music_group_500_no_candidate_returns_play_failed(
    tmp_path, monkeypatch
):
    db = _db(tmp_path)
    calls = _seq_play(monkeypatch, [{"ok": False, "error": "HA 500: group reject"}])
    play = _play_tool_fallback(db, "mdopp", _FakeJellyfin(), [])  # no same-area device
    out = json.loads(
        await play.handler(
            {"title": "Bohemian Rhapsody", "entity_id": "media_player.wohnzimmer"}
        )
    )
    # Honest failure — no fake success.
    assert out["ok"] is False and out["reason"] == "play_failed"
    assert out["detail"] == "HA 500: group reject"
    # Only the original target's static+universal retries, no fallback cast.
    assert all(c[2] == "media_player.wohnzimmer" for c in calls)


async def test_play_music_not_found_does_not_trigger_fallback(tmp_path, monkeypatch):
    db = _db(tmp_path)
    # A track-not-found never reaches a cast → the fallback can't fire (no 500).
    calls = _seq_play(monkeypatch, [{"ok": True, "state": "playing"}])
    fallback_calls: list[str] = []

    async def _area_fallback(entity_id):
        fallback_calls.append(entity_id)
        return ["media_player.home_assistant_voice_0907c9_media_player"]

    tools = build_music_query_tools(
        db,
        lambda: "mdopp",
        _FakeJellyfin(),
        hass_url="http://ha",
        hass_token="tok",
        area_fallback=_area_fallback,
    )
    (play,) = [t for t in tools if t.name == "play_music"]
    out = json.loads(
        await play.handler(
            {"title": "Xqzptv Nonsense", "entity_id": "media_player.wohnzimmer"}
        )
    )
    assert out["ok"] is False and out["reason"] == "not_found"
    assert calls == []  # no cast at all
    assert fallback_calls == []  # fallback never consulted


async def test_play_music_single_device_success_no_fallback(tmp_path, monkeypatch):
    db = _db(tmp_path)
    calls = _seq_play(monkeypatch, [{"ok": True, "state": "playing"}])
    fallback_calls: list[str] = []

    async def _area_fallback(entity_id):
        fallback_calls.append(entity_id)
        return ["media_player.home_assistant_voice_0907c9_media_player"]

    tools = build_music_query_tools(
        db,
        lambda: "mdopp",
        _FakeJellyfin(),
        hass_url="http://ha",
        hass_token="tok",
        area_fallback=_area_fallback,
    )
    (play,) = [t for t in tools if t.name == "play_music"]
    out = json.loads(
        await play.handler(
            {"title": "Bohemian Rhapsody", "entity_id": "media_player.kuche"}
        )
    )
    # Küche plays directly on the first cast → no fallback consulted.
    assert out["ok"] is True and out["entity_id"] == "media_player.kuche"
    assert fallback_calls == []
    assert len(calls) == 1


async def test_play_music_description_states_filler_rule(tmp_path):
    db = _db(tmp_path)
    desc = _play_tool(db, "mdopp", _FakeJellyfin()).description
    assert "NUR der Songtitel" in desc
    assert "Füllwörter" in desc
    assert "Zufallssong" in desc


async def test_play_music_description_steers_music_not_podcast(tmp_path):
    db = _db(tmp_path)
    desc = _play_tool(db, "mdopp", _FakeJellyfin()).description
    assert "play_music" not in desc  # describes itself, doesn't name itself
    assert "Spiele Musik" in desc
    assert "NIE media_find_podcast" in desc


# ---- playlist_add (#647): write a track to a Jellyfin playlist ---------------


class _FakePlaylistJellyfin:
    """A fake Jellyfin client for playlist_add: records the playlist writes and
    serves an in-memory playlist store."""

    def __init__(self, *, existing=None):
        # {playlist_id: (name, [audio_ids])}
        self._store = dict(existing or {})
        self.created: list[tuple[str, list[str]]] = []
        self.added: list[tuple[str, list[str]]] = []

    async def lyrics(self, audio_id: str):
        return None

    async def stream_url(self, audio_id: str, *, static: bool = True):
        return f"http://jf/{audio_id}"

    async def playlists(self):
        return [(pid, name) for pid, (name, _ids) in self._store.items()]

    async def create_playlist(self, name: str, ids: list[str]) -> str:
        pid = f"pl-{len(self._store) + 1}"
        self._store[pid] = (name, list(ids))
        self.created.append((name, list(ids)))
        return pid

    async def playlist_add(self, playlist_id: str, ids: list[str]) -> bool:
        name, existing = self._store[playlist_id]
        self._store[playlist_id] = (name, existing + list(ids))
        self.added.append((playlist_id, list(ids)))
        return True


class _FakeRecorder:
    """Mimics TraceRecorder.list_traces() (newest first) over injected steps."""

    def __init__(self, steps):
        self._steps = list(steps)

    def list_traces(self):
        return list(self._steps)[::-1]


def _play_step(session_id, *, ok=True, audio_id="aud-boh", title="Bohemian Rhapsody"):
    out = {"ok": ok, "title": title}
    if audio_id is not None:
        out["audio_id"] = audio_id
    return {
        "step_kind": "tool",
        "session_id": session_id,
        "tool_name": "play_music",
        "output": json.dumps(out, ensure_ascii=False),
    }


def _playlist_tool(db, uid, client, *, recorder=None, session=""):
    tools = build_music_query_tools(
        db,
        lambda: uid,
        client,
        recorder=recorder,
        session_getter=(lambda: session),
    )
    (t,) = [t for t in tools if t.name == "playlist_add"]
    return t


async def _call_playlist(db, uid, args, client, *, recorder=None, session=""):
    t = _playlist_tool(db, uid, client, recorder=recorder, session=session)
    return json.loads(await t.handler(args))


async def test_playlist_add_registered_on_client_alone(tmp_path):
    db = _db(tmp_path)
    names = {
        t.name for t in build_music_query_tools(db, lambda: "mdopp", _FakeJellyfin())
    }
    assert "playlist_add" in names
    # No client -> not registered.
    names = {t.name for t in build_music_query_tools(db, lambda: "mdopp")}
    assert "playlist_add" not in names


async def test_playlist_add_explicit_track_creates_default(tmp_path):
    db = _db(tmp_path)
    client = _FakePlaylistJellyfin()
    out = await _call_playlist(db, "mdopp", {"track": "Bohemian Rhapsody"}, client)
    assert out["ok"] is True
    assert out["title"] == "Bohemian Rhapsody"
    assert out["playlist"] == "Favoriten"
    assert client.created == [("Favoriten", ["aud-boh"])]


async def test_playlist_add_explicit_track_appends_existing(tmp_path):
    db = _db(tmp_path)
    client = _FakePlaylistJellyfin(existing={"pl-x": ("favoriten", ["seed"])})
    out = await _call_playlist(db, "mdopp", {"track": "Bohemian Rhapsody"}, client)
    assert out["ok"] is True and out["playlist"] == "Favoriten"
    # Case-insensitive match on the existing playlist -> append, no create.
    assert client.created == []
    assert client.added == [("pl-x", ["aud-boh"])]


async def test_playlist_add_named_playlist(tmp_path):
    db = _db(tmp_path)
    client = _FakePlaylistJellyfin()
    out = await _call_playlist(
        db, "mdopp", {"track": "Bohemian Rhapsody", "playlist": "Party"}, client
    )
    assert out["ok"] is True and out["playlist"] == "Party"
    assert client.created == [("Party", ["aud-boh"])]


async def test_playlist_add_unknown_track_say(tmp_path):
    db = _db(tmp_path)
    client = _FakePlaylistJellyfin()
    out = await _call_playlist(db, "mdopp", {"track": "xqzptv nonsense"}, client)
    assert out["ok"] is False and out["reason"] == "not_found"
    assert "?" in out["say"]
    assert client.created == [] and client.added == []


async def test_playlist_add_last_played_from_recorder(tmp_path):
    db = _db(tmp_path)
    client = _FakePlaylistJellyfin()
    recorder = _FakeRecorder([_play_step("sess-1", audio_id="aud-boh")])
    out = await _call_playlist(
        db, "mdopp", {}, client, recorder=recorder, session="sess-1"
    )
    assert out["ok"] is True
    assert out["title"] == "Bohemian Rhapsody"
    assert client.created == [("Favoriten", ["aud-boh"])]


async def test_playlist_add_no_current_track_say(tmp_path):
    db = _db(tmp_path)
    client = _FakePlaylistJellyfin()
    # Empty recorder -> nothing played this session.
    recorder = _FakeRecorder([])
    out = await _call_playlist(
        db, "mdopp", {}, client, recorder=recorder, session="sess-1"
    )
    assert out["ok"] is False and out["reason"] == "no_current_track"
    assert "?" in out["say"]


async def test_playlist_add_pre_extension_record_no_audio_id(tmp_path):
    db = _db(tmp_path)
    client = _FakePlaylistJellyfin()
    # A play_music step recorded before #645 carries no audio_id -> skip it
    # gracefully rather than crash, and report no_current_track.
    recorder = _FakeRecorder([_play_step("sess-1", audio_id=None)])
    out = await _call_playlist(
        db, "mdopp", {}, client, recorder=recorder, session="sess-1"
    )
    assert out["ok"] is False and out["reason"] == "no_current_track"


async def test_playlist_add_last_played_scoped_to_session(tmp_path):
    db = _db(tmp_path)
    client = _FakePlaylistJellyfin()
    # A play in a DIFFERENT session must not leak into this one.
    recorder = _FakeRecorder([_play_step("other-sess", audio_id="aud-boh")])
    out = await _call_playlist(
        db, "mdopp", {}, client, recorder=recorder, session="sess-1"
    )
    assert out["ok"] is False and out["reason"] == "no_current_track"


async def test_playlist_add_no_client_no_stream(tmp_path):
    db = _db(tmp_path)
    # A client is required to register the tool, but guard the handler anyway.
    tools = build_music_query_tools(db, lambda: "mdopp")
    assert not [t for t in tools if t.name == "playlist_add"]


async def test_playlist_add_description_steers(tmp_path):
    db = _db(tmp_path)
    (t,) = [
        t
        for t in build_music_query_tools(db, lambda: "mdopp", _FakePlaylistJellyfin())
        if t.name == "playlist_add"
    ]
    assert "ZULETZT GESPIELTE" in t.description
    assert "NICHT zum Abspielen" in t.description
    assert "Playlist" in t.description

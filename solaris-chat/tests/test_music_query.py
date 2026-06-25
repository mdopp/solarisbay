"""music_query tool — structured artist→songs over entities/facts (#588).

Security-critical: every query is per-owner scoped (`resident_uid IN (caller,
'household')`). The artist match is EXACT-then-prefix, never a bare substring, so
"Queen" never returns "Queens of the Stone Age". Titles are the clean
`canonical_name`, never the hash slug.
"""

from __future__ import annotations

import json
import sqlite3

from solaris_chat.engine.tools.music_query import build_music_query_tools

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
    (t,) = build_music_query_tools(db, lambda: uid, client)
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
    assert "was weiß ich über" in desc


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
    assert "Lyrics" in desc

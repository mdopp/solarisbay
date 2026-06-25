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


def _band(conn, ent_id, name, slug, owner, *, okf_prefix="okf"):
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


def _song(conn, ent_id, title, band_slug, owner):
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


def _db(tmp_path) -> str:
    path = str(tmp_path / "solaris.db")
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA)
    # Two household bands whose names collide on a substring.
    _band(conn, "b-queen", "Queen", "queen", "household")
    _song(conn, "s-bohemian", "Bohemian Rhapsody", "queen", "household")
    _song(conn, "s-radio", "Radio Ga Ga", "queen", "household")
    _band(
        conn,
        "b-qotsa",
        "Queens of the Stone Age",
        "queens-of-the-stone-age",
        "household",
    )
    _song(conn, "s-nomone", "No One Knows", "queens-of-the-stone-age", "household")
    # A cdopp-private band+song (a private Jellyfin library, users/cdopp/okf/...).
    _band(
        conn,
        "b-private",
        "Tocotronic",
        "tocotronic",
        "cdopp",
        okf_prefix="users/cdopp/okf",
    )
    _song(conn, "s-priv", "Pure Vernunft", "tocotronic", "cdopp")
    conn.commit()
    conn.close()
    return path


def _tool(db, uid):
    (t,) = build_music_query_tools(db, lambda: uid)
    return t


async def _call(db, uid, args):
    return json.loads(await _tool(db, uid).handler(args))


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


async def test_bad_op(tmp_path):
    db = _db(tmp_path)
    out = await _call(db, "mdopp", {"op": "nonsense"})
    assert "error" in out

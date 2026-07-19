"""Chat-derived music affinity → `used_to_love` album facts (#881, B9, ADR 0003).

Covers the two halves of the P2c Stenograph extension:
  - `extract_affinities`: the deterministic parse of past-music-love phrasings
    ("das war früher mein Lieblingsalbum X von Y", "X von Y rauf und runter
    gehört") into (artist, album, memory), user-turns only, deduped;
  - `route_affinities`: resolve/create the album entity by P1a's "Artist – Album"
    canonical_name/slug and write a source-tagged (`stenograph`) `used_to_love`
    fact at a soft confidence — coexisting with a Jellyfin `by` edge without
    clobbering, per-resident, idempotent — plus a Musik-Erinnerung note when the
    turn carries narrative, and the P2a wishlist surfacing the routed fact.

Schema mirrors the #446 migration (inlined, as in the sibling ingest tests).
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from solaris_chat.engine import music_affinity
from solaris_chat.engine.knowledge import projection
from solaris_chat.engine.knowledge.writer import OkfWriter
from solaris_chat.engine.tools.music_query import build_music_query_tools

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
"""


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


def _facts(db_path, name):
    conn = projection.open_conn(db_path)
    try:
        row = conn.execute(
            "SELECT id FROM entities WHERE type = 'album' AND canonical_name = ?",
            (name,),
        ).fetchone()
        if row is None:
            return None
        return {
            (f["predicate"], f["value"], f["source"], f["confidence"])
            for f in conn.execute(
                "SELECT predicate, value, source, confidence FROM facts"
                " WHERE subject_entity_id = ?",
                (row["id"],),
            ).fetchall()
        }
    finally:
        conn.close()


# --- extraction --------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Dummy von Portishead war früher mein Lieblingsalbum.",
        'Mein Lieblingsalbum war früher "Dummy" von Portishead.',
        "Dummy von Portishead hab ich früher rauf und runter gehört.",
        "Früher hab ich Dummy von Portishead rauf und runter gehört.",
        "Ich habe Dummy von Portishead früher geliebt.",
    ],
)
def test_extract_recognizes_past_love_phrasings(text):
    affs = music_affinity.extract_affinities([("user", text)])
    assert len(affs) == 1
    assert affs[0].artist == "Portishead"
    assert affs[0].album == "Dummy"


def test_extract_only_user_turns_and_dedups():
    msgs = [
        ("assistant", "Dummy von Portishead war früher mein Lieblingsalbum."),
        ("user", "Dummy von Portishead war früher mein Lieblingsalbum."),
        ("user", "Dummy von Portishead hab ich rauf und runter gehört."),
    ]
    affs = music_affinity.extract_affinities(msgs)
    # Assistant paraphrase ignored; the two user mentions of the same (artist,
    # album) collapse to one affinity.
    assert len(affs) == 1
    assert (affs[0].artist, affs[0].album) == ("Portishead", "Dummy")


def test_extract_ignores_neutral_present_mention():
    # No nostalgia trigger → not an affinity (a plain "ich höre X von Y").
    affs = music_affinity.extract_affinities(
        [("user", "Ich höre gerade Dummy von Portishead.")]
    )
    assert affs == []


# --- routing → used_to_love fact ---------------------------------------------


def test_route_creates_album_with_soft_used_to_love_fact(env):
    writer, db_path, _ = env
    affs = music_affinity.extract_affinities(
        [("user", "Dummy von Portishead war früher mein Lieblingsalbum.")]
    )
    written = music_affinity.route_affinities(writer, db_path, "mdopp", affs)
    assert written == 1
    # The album entity was CREATED (chat-loved, neither owned nor digital) with a
    # soft-confidence stenograph-sourced used_to_love fact.
    assert _facts(db_path, "Portishead – Dummy") == {
        ("used_to_love", "", "stenograph", 0.5)
    }
    # Per-resident: scoped to the owner, not household.
    assert music_affinity.album_used_to_love(db_path, "mdopp") == ["Portishead – Dummy"]
    assert music_affinity.album_used_to_love(db_path, "lena") == []


def test_route_coexists_with_jellyfin_fact_without_clobbering(env):
    writer, db_path, _ = env
    # A Jellyfin album already exists (same "Artist – Album" canonical_name/slug)
    # with a `by` edge (source=jellyfin). The chat affinity must attach
    # used_to_love to the SAME entity and leave the Jellyfin fact intact.
    conn = projection.open_conn(db_path)
    conn.execute(
        "INSERT INTO entities (id, type, canonical_name, resident_uid, source,"
        " content_hash) VALUES ('al-1', 'album', 'Portishead – Dummy',"
        " 'mdopp', 'jellyfin', 'h')"
    )
    conn.execute(
        "INSERT INTO facts (id, subject_entity_id, resident_uid, predicate, value,"
        " source) VALUES ('f-by', 'al-1', 'mdopp', 'by', 'bands/portishead',"
        " 'jellyfin')"
    )
    conn.commit()
    conn.close()
    affs = music_affinity.extract_affinities(
        [("user", "Dummy von Portishead war früher mein Lieblingsalbum.")]
    )
    music_affinity.route_affinities(writer, db_path, "mdopp", affs)
    # Same entity (no dup album), Jellyfin `by` edge preserved, used_to_love added.
    conn = projection.open_conn(db_path)
    assert (
        conn.execute(
            "SELECT COUNT(*) AS n FROM entities WHERE type = 'album'"
        ).fetchone()["n"]
        == 1
    )
    conn.close()
    assert _facts(db_path, "Portishead – Dummy") == {
        ("by", "bands/portishead", "jellyfin", None),
        ("used_to_love", "", "stenograph", 0.5),
    }


def test_route_is_idempotent(env):
    writer, db_path, _ = env
    affs = music_affinity.extract_affinities(
        [("user", "Dummy von Portishead war früher mein Lieblingsalbum.")]
    )
    assert music_affinity.route_affinities(writer, db_path, "mdopp", affs) == 1
    # A second unchanged run short-circuits (ingest_log) — nothing re-written.
    assert music_affinity.route_affinities(writer, db_path, "mdopp", affs) == 0
    assert _facts(db_path, "Portishead – Dummy") == {
        ("used_to_love", "", "stenograph", 0.5)
    }


def test_route_writes_musik_erinnerung_note_when_narrative(env):
    writer, db_path, tmp_path = env
    memory = (
        "Dummy von Portishead war früher mein Lieblingsalbum, weil wir das immer"
        " im Auto vom Urlaub gehört haben."
    )
    affs = music_affinity.extract_affinities([("user", memory)])
    music_affinity.route_affinities(writer, db_path, "mdopp", affs)
    note_md = (
        tmp_path
        / "notes"
        / "users"
        / "mdopp"
        / "okf"
        / "notes"
        / "musik-erinnerung-portishead-dummy.md"
    )
    assert note_md.is_file()
    text = note_md.read_text()
    assert "im Auto vom Urlaub" in text
    assert "[[albums/portishead-dummy]]" in text  # linked to the album entity


def test_route_no_note_without_narrative(env):
    writer, db_path, tmp_path = env
    affs = music_affinity.extract_affinities(
        [("user", "Dummy von Portishead war früher mein Lieblingsalbum.")]
    )
    music_affinity.route_affinities(writer, db_path, "mdopp", affs)
    # A bare affinity (no story) → the fact, but no Musik-Erinnerung note.
    assert not (tmp_path / "notes" / "users" / "mdopp" / "okf" / "notes").exists()


# --- wishlist surfacing (P2a) ------------------------------------------------


async def test_routed_affinity_surfaces_in_wishlist(env):
    writer, db_path, _ = env
    affs = music_affinity.extract_affinities(
        [("user", "Dummy von Portishead war früher mein Lieblingsalbum.")]
    )
    music_affinity.route_affinities(writer, db_path, "mdopp", affs)
    # The P2a wishlist query: a chat-loved album that is neither owned physically
    # nor digitally present is an acquire candidate.
    (tool,) = build_music_query_tools(db_path, lambda: "mdopp")
    out = json.loads(await tool.handler({"op": "wishlist"}))
    assert out["albums"] == ["Portishead – Dummy"]

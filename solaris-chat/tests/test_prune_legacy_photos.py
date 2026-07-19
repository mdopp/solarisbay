"""Prune legacy per-photo OKF artifacts (ADR 0002/B7).

Immich photos are events; making them projection-only (like Jellyfin songs)
means no per-photo markdown belongs in the vault. These tests prove: (1) a photo
event's markdown + `concepts` + `okf_vectors` + FTS rows are pruned while its
events-table row + `event_entities` edges remain; (2) a non-photo event (a trip
that keeps its markdown) is untouched; (3) a second run is a no-op (idempotent).

Schema is inlined DDL mirroring the #016/#446/#877 migrations (importing alembic
from a chat test fails CI's clean env).
"""

from __future__ import annotations

import sqlite3

import pytest

from solaris_chat import notes_index
from solaris_chat.engine.ingest.prune import prune_legacy_photo_artifacts

_SCHEMA = """
CREATE TABLE entities (
  id TEXT PRIMARY KEY, type TEXT NOT NULL, canonical_name TEXT NOT NULL,
  resident_uid TEXT NOT NULL, source TEXT NOT NULL, content_hash TEXT NOT NULL,
  updated TEXT NOT NULL DEFAULT (datetime('now')));
CREATE TABLE events (
  id TEXT PRIMARY KEY, ts TEXT NOT NULL, resident_uid TEXT NOT NULL,
  kind TEXT NOT NULL, source TEXT NOT NULL);
CREATE TABLE event_entities (
  event_id TEXT NOT NULL, entity_id TEXT NOT NULL, role TEXT NOT NULL,
  PRIMARY KEY (event_id, entity_id, role));
CREATE TABLE concepts (
  id TEXT PRIMARY KEY, ref_id TEXT NOT NULL,
  ref_kind TEXT NOT NULL CHECK (ref_kind IN ('entity', 'event')),
  okf_path TEXT NOT NULL, embedding_id TEXT, content_hash TEXT NOT NULL,
  updated TEXT NOT NULL DEFAULT (datetime('now')));
CREATE TABLE okf_vectors (
  embedding_id TEXT PRIMARY KEY, concept_id TEXT NOT NULL, model TEXT NOT NULL,
  dim INTEGER NOT NULL, vector BLOB NOT NULL,
  updated TEXT NOT NULL DEFAULT (datetime('now')));
"""

_EVENTS_DIR = ("users", "mdopp", "okf", "events", "2026")


def _entity(conn, eid, etype, source="immich", resident="mdopp"):
    conn.execute(
        "INSERT INTO entities (id, type, canonical_name, resident_uid, source,"
        " content_hash) VALUES (?, ?, ?, ?, ?, 'h')",
        (eid, etype, eid, resident, source),
    )


def _event(conn, eid, kind, source="immich", resident="mdopp"):
    conn.execute(
        "INSERT INTO events (id, ts, resident_uid, kind, source)"
        " VALUES (?, '2026-05-30T10:00:00', ?, ?, ?)",
        (eid, resident, kind, source),
    )


def _event_entity(conn, event_id, entity_id, role):
    conn.execute(
        "INSERT INTO event_entities (event_id, entity_id, role) VALUES (?, ?, ?)",
        (event_id, entity_id, role),
    )


def _concept(conn, cid, ref_id, okf_path, embedding_id):
    conn.execute(
        "INSERT INTO concepts (id, ref_id, ref_kind, okf_path, embedding_id,"
        " content_hash) VALUES (?, ?, 'event', ?, ?, 'h')",
        (cid, ref_id, okf_path, embedding_id),
    )


def _vector(conn, embedding_id, concept_id):
    conn.execute(
        "INSERT INTO okf_vectors (embedding_id, concept_id, model, dim, vector)"
        " VALUES (?, ?, 'nomic-embed-text', 1, ?)",
        (embedding_id, concept_id, b"\x00\x00\x00\x00"),
    )


@pytest.fixture
def legacy_photo_env(tmp_path):
    """A vault + db as it looked BEFORE photos went projection-only: an Immich
    photo event with markdown + concepts + okf_vectors + a depicted edge, plus a
    non-photo trip event that keeps its full markdown."""
    db_path = str(tmp_path / "solaris.db")
    notes_dir = tmp_path / "notes"
    events_dir = notes_dir.joinpath(*_EVENTS_DIR)
    events_dir.mkdir(parents=True)
    photo_rel = "/".join((*_EVENTS_DIR, "2026-05-30-seaside-a1.md"))
    trip_rel = "/".join((*_EVENTS_DIR, "2026-05-30-climbing-trip.md"))
    photo_md = notes_dir / photo_rel
    trip_md = notes_dir / trip_rel
    photo_md.write_text("---\nkind: photo\ntitle: Seaside\n---\n", encoding="utf-8")
    trip_md.write_text("---\nkind: trip\ntitle: Climbing\n---\n", encoding="utf-8")

    conn = sqlite3.connect(db_path)
    conn.executescript(_SCHEMA)
    _entity(conn, "anna", "person")
    _event(conn, "ev-photo", "photo")
    _event(conn, "ev-trip", "trip")
    _event_entity(conn, "ev-photo", "anna", "depicted")
    _event_entity(conn, "ev-trip", "anna", "with")
    _concept(conn, "c-photo", "ev-photo", photo_rel, "e-photo")
    _concept(conn, "c-trip", "ev-trip", trip_rel, "e-trip")
    _vector(conn, "e-photo", "ev-photo")
    _vector(conn, "e-trip", "ev-trip")
    notes_index.ensure_schema(conn)
    notes_index.index_note(conn, notes_dir, photo_rel)
    notes_index.index_note(conn, notes_dir, trip_rel)
    conn.commit()
    conn.close()
    return db_path, notes_dir, photo_md, trip_md


def test_prune_removes_photo_markdown_concepts_and_embedding(legacy_photo_env):
    db_path, notes_dir, photo_md, _trip = legacy_photo_env

    pruned = prune_legacy_photo_artifacts(db_path, str(notes_dir))

    assert pruned == 1
    assert not photo_md.exists()
    conn = sqlite3.connect(db_path)
    try:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM concepts WHERE ref_id = 'ev-photo'"
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM okf_vectors WHERE embedding_id = 'e-photo'"
            ).fetchone()[0]
            == 0
        )
        assert notes_index.search(conn, "seaside") == []  # FTS row swept too
    finally:
        conn.close()


def test_prune_keeps_photo_event_row_and_edges(legacy_photo_env):
    from solaris_chat.engine.knowledge import okf

    db_path, notes_dir, *_ = legacy_photo_env
    det = okf.deterministic_id("/".join((*_EVENTS_DIR, "2026-05-30-seaside-a1.md")))

    prune_legacy_photo_artifacts(db_path, str(notes_dir))

    conn = sqlite3.connect(db_path)
    try:
        # Both events survive; the photo is re-keyed to the deterministic id ...
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 2
        assert (
            conn.execute("SELECT COUNT(*) FROM events WHERE id = ?", (det,)).fetchone()[
                0
            ]
            == 1
        )
        # ... with its depicted edge intact under the new id.
        assert (
            conn.execute(
                "SELECT entity_id FROM event_entities"
                " WHERE event_id = ? AND role = 'depicted'",
                (det,),
            ).fetchone()[0]
            == "anna"
        )
    finally:
        conn.close()


def test_prune_leaves_non_photo_event_untouched(legacy_photo_env):
    db_path, notes_dir, _photo, trip_md = legacy_photo_env

    prune_legacy_photo_artifacts(db_path, str(notes_dir))

    assert trip_md.exists()
    conn = sqlite3.connect(db_path)
    try:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM concepts WHERE ref_id = 'ev-trip'"
            ).fetchone()[0]
            == 1
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM okf_vectors WHERE embedding_id = 'e-trip'"
            ).fetchone()[0]
            == 1
        )
        assert notes_index.search(conn, "climbing") != []
    finally:
        conn.close()


def test_prune_is_idempotent(legacy_photo_env):
    db_path, notes_dir, *_ = legacy_photo_env

    assert prune_legacy_photo_artifacts(db_path, str(notes_dir)) == 1
    # A pruned photo has no concepts row, so the second pass matches nothing.
    assert prune_legacy_photo_artifacts(db_path, str(notes_dir)) == 0

"""Year-sharding of `okf/events/` — write path, dedup fallback, migration (#830b).

The 830a FTS schema + the OKF projection schema are owned by alembic in
`database/`; importing alembic from a chat test fails CI's clean env, so these
build the tables from the in-module DDL mirrors (`notes_index.ensure_schema`
and the writer's own mirror below).
"""

from __future__ import annotations

import sqlite3

import pytest

from solaris_chat import notes_index
from solaris_chat.engine.knowledge import ConceptRecord, PendingEmbeddingQueue
from solaris_chat.engine.knowledge import okf, projection
from solaris_chat.engine.knowledge.writer import OkfWriter
from solaris_chat.scripts import migrate_events_sharding as mig


# Mirrors database/migrations (okf index) — same as test_okf_writer._SCHEMA.
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
CREATE TABLE facts (
  id TEXT PRIMARY KEY, subject_entity_id TEXT, resident_uid TEXT NOT NULL,
  predicate TEXT NOT NULL, value TEXT NOT NULL, confidence REAL,
  source TEXT NOT NULL, timestamp TEXT NOT NULL DEFAULT (datetime('now')));
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
    notes_index.ensure_schema(conn)
    conn.commit()
    conn.close()
    queue = PendingEmbeddingQueue(db_path)
    writer = OkfWriter(db_path=db_path, notes_dir=notes_dir, embedding_queue=queue)
    return writer, db_path, tmp_path


def _event(title="Photo 1", asset="a1", when="2024-05-01T10:00:00") -> ConceptRecord:
    # Mirrors the immich adapter's event record shape (asset-id suffixed slug).
    return ConceptRecord(
        type="event",
        title=title,
        slug=f"{title.lower().replace(' ', '-')}-{asset}",
        source="immich",
        external_id=f"asset/{asset}",
        resident="household",
        timestamp=when,
        event_ts=when,
        event_kind="photo",
        body=f"Immich asset {asset}.",
    )


# --- 1. sharded write path ---------------------------------------------------


def test_new_event_note_lands_in_year_subdir(env):
    writer, _, tmp_path = env
    res = writer.write_concept(_event(when="2024-05-01T10:00:00"))
    assert res.okf_path == "okf/events/2024/2024-05-01-photo-1-a1.md"
    assert (tmp_path / "notes" / res.okf_path).is_file()


def test_okf_path_year_from_event_ts():
    rec = _event(when="2019-12-31T23:59:59")
    assert okf.okf_path(rec) == "okf/events/2019/2019-12-31-photo-1-a1.md"


def test_okf_path_event_without_date_stays_flat():
    # No event_ts/timestamp → no year segment (can't invent one).
    rec = _event(when="")
    assert okf.okf_path(rec) == "okf/events/photo-1-a1.md"


# --- 2. _existing_event_id slug fallback (no duplicate on re-ingest) ----------


def test_reingest_of_sharded_event_is_skipped_no_duplicate(env):
    writer, db_path, _ = env
    writer.write_concept(_event())
    second = writer.write_concept(_event())  # identical asset → unchanged
    assert second.skipped is True
    conn = projection.open_conn(db_path)
    assert projection.row_count(conn, "concepts") == 1
    conn.close()


def test_existing_event_id_finds_note_at_old_flat_path(env):
    """A concept row still at the pre-migration flat path must be found by the
    slug fallback, so a re-ingest reuses its id instead of minting a duplicate."""
    writer, db_path, _ = env
    rec = _event()
    ref_id = "flat-event-id"
    # Simulate a pre-migration row: the note sits at the FLAT path, sharded write
    # would target okf/events/2024/... .
    conn = projection.open_conn(db_path)
    conn.execute(
        "INSERT INTO concepts (id, ref_id, ref_kind, okf_path, content_hash) "
        "VALUES ('c1', ?, 'event', 'okf/events/2024-05-01-photo-1-a1.md', 'h')",
        (ref_id,),
    )
    conn.commit()
    assert writer._existing_event_id(conn, rec) == ref_id
    conn.close()


def test_existing_event_id_finds_note_at_new_sharded_path(env):
    writer, db_path, _ = env
    rec = _event()
    ref_id = "sharded-event-id"
    conn = projection.open_conn(db_path)
    conn.execute(
        "INSERT INTO concepts (id, ref_id, ref_kind, okf_path, content_hash) "
        "VALUES ('c1', ?, 'event', 'okf/events/2024/2024-05-01-photo-1-a1.md', 'h')",
        (ref_id,),
    )
    conn.commit()
    assert writer._existing_event_id(conn, rec) == ref_id
    conn.close()


def test_existing_event_id_does_not_cross_match_a_different_leaf(env):
    writer, db_path, _ = env
    conn = projection.open_conn(db_path)
    conn.execute(
        "INSERT INTO concepts (id, ref_id, ref_kind, okf_path, content_hash) "
        "VALUES ('c1', 'other', 'event', 'okf/events/2024/2024-05-01-other-x9.md', 'h')"
    )
    conn.commit()
    assert writer._existing_event_id(conn, _event()) is None
    conn.close()


# --- 3. migration: moves flat → year subdir AND re-points the FTS row ---------


def _seed_flat_note(root, db_path, rel, ref_id, *, ts_line=True):
    body = "---\ntype: event\n"
    if ts_line:
        body += "timestamp: 2024-05-01T10:00:00\n"
    body += "---\n\nWeinfest am See.\n"
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO concepts (id, ref_id, ref_kind, okf_path, content_hash) "
        "VALUES (?, ?, 'event', ?, 'h')",
        (ref_id, ref_id, rel),
    )
    notes_index.ensure_schema(conn)
    notes_index.index_note(conn, root, rel)
    conn.commit()
    conn.close()


def test_migration_moves_note_and_keeps_it_searchable(env):
    _, db_path, tmp_path = env
    root = tmp_path / "notes"
    rel = "okf/events/2024-05-01-weinfest-a1.md"
    _seed_flat_note(root, db_path, rel, "e1")

    conn = sqlite3.connect(db_path)
    assert notes_index.search(conn, "weinfest") == [rel]  # findable pre-move
    conn.close()

    stats = mig.migrate(db_path, str(root))
    assert stats["moved"] == 1
    new_rel = "okf/events/2024/2024-05-01-weinfest-a1.md"
    assert (root / new_rel).is_file()
    assert not (root / rel).exists()  # nothing left behind except via the rename

    conn = sqlite3.connect(db_path)
    try:
        # concepts.okf_path re-pointed
        assert (
            conn.execute("SELECT okf_path FROM concepts WHERE ref_id='e1'").fetchone()[
                0
            ]
            == new_rel
        )
        # CRITICAL: FTS row moved with it — search finds the NEW path, not the old.
        assert notes_index.search(conn, "weinfest") == [new_rel]
    finally:
        conn.close()


def test_migration_is_idempotent(env):
    _, db_path, tmp_path = env
    root = tmp_path / "notes"
    _seed_flat_note(root, db_path, "okf/events/2024-05-01-weinfest-a1.md", "e1")
    first = mig.migrate(db_path, str(root))
    assert first["moved"] == 1
    second = mig.migrate(db_path, str(root))  # already sharded → no-op
    assert second["moved"] == 0
    # The note now lives under `events/<year>/`; the scanner only visits files
    # directly in `events/`, so an already-sharded note is simply not re-scanned.
    assert second["scanned"] == 0
    # The file and its re-pointed row are untouched by the second pass.
    new_rel = "okf/events/2024/2024-05-01-weinfest-a1.md"
    assert (root / new_rel).is_file()
    conn = sqlite3.connect(db_path)
    assert notes_index.search(conn, "weinfest") == [new_rel]
    conn.close()


def test_migration_year_fallback_from_filename_when_no_frontmatter_date(env):
    _, db_path, tmp_path = env
    root = tmp_path / "notes"
    rel = "okf/events/2021-08-09-fest-a1.md"
    _seed_flat_note(root, db_path, rel, "e1", ts_line=False)
    stats = mig.migrate(db_path, str(root))
    assert stats["moved"] == 1
    assert (root / "okf/events/2021/2021-08-09-fest-a1.md").is_file()


def test_migration_skips_note_with_no_derivable_year(env):
    _, db_path, tmp_path = env
    root = tmp_path / "notes"
    rel = "okf/events/undated-slug-a1.md"  # no frontmatter ts, no date prefix
    (root / "okf/events").mkdir(parents=True)
    (root / rel).write_text("---\ntype: event\n---\n\nx\n", encoding="utf-8")
    stats = mig.migrate(db_path, str(root))
    assert stats["moved"] == 0 and stats["no_year"] == 1
    assert (root / rel).is_file()  # left untouched


def test_migration_dry_run_moves_nothing(env):
    _, db_path, tmp_path = env
    root = tmp_path / "notes"
    rel = "okf/events/2024-05-01-weinfest-a1.md"
    _seed_flat_note(root, db_path, rel, "e1")
    stats = mig.migrate(db_path, str(root), dry_run=True)
    assert stats["moved"] == 1  # reported
    assert (root / rel).is_file()  # but not actually moved
    conn = sqlite3.connect(db_path)
    assert (
        conn.execute("SELECT okf_path FROM concepts WHERE ref_id='e1'").fetchone()[0]
        == rel
    )
    conn.close()


def test_migration_also_shards_user_scoped_events(env):
    _, db_path, tmp_path = env
    root = tmp_path / "notes"
    rel = "users/mdopp/okf/events/2024-05-01-weinfest-a1.md"
    _seed_flat_note(root, db_path, rel, "e1")
    mig.migrate(db_path, str(root))
    assert (root / "users/mdopp/okf/events/2024/2024-05-01-weinfest-a1.md").is_file()

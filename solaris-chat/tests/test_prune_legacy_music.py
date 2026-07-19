"""Prune legacy per-item Jellyfin OKF artifacts (#878, ADR 0002/B7).

Songs, albums, and bands are all projection-only now: each keeps its entity +
facts, but no per-item markdown / `concepts` link row / `okf_vectors` embedding.
These tests prove: (1) the markdown + concepts + vectors rows are gone for all
three while the entities + facts (incl. a band `bio`) remain; (2) an orphaned
stub under a domain dir is swept; (3) a second run is a no-op (idempotent).

Schema is inlined DDL mirroring the #446/#877 migrations (importing alembic from
a chat test fails CI's clean env).
"""

from __future__ import annotations

import sqlite3

import pytest

from solaris_chat import notes_index
from solaris_chat.engine.ingest.prune import prune_legacy_music_artifacts

_SCHEMA = """
CREATE TABLE entities (
  id TEXT PRIMARY KEY, type TEXT NOT NULL, canonical_name TEXT NOT NULL,
  resident_uid TEXT NOT NULL, source TEXT NOT NULL, content_hash TEXT NOT NULL,
  updated TEXT NOT NULL DEFAULT (datetime('now')));
CREATE TABLE facts (
  id TEXT PRIMARY KEY, subject_entity_id TEXT, resident_uid TEXT NOT NULL,
  predicate TEXT NOT NULL, value TEXT NOT NULL, confidence REAL,
  source TEXT NOT NULL, timestamp TEXT NOT NULL DEFAULT (datetime('now')),
  FOREIGN KEY (subject_entity_id) REFERENCES entities (id));
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


def _entity(conn, eid, etype, source="jellyfin", resident="household"):
    conn.execute(
        "INSERT INTO entities (id, type, canonical_name, resident_uid, source,"
        " content_hash) VALUES (?, ?, ?, ?, ?, 'h')",
        (eid, etype, eid, resident, source),
    )


def _fact(conn, fid, subject, predicate, value):
    conn.execute(
        "INSERT INTO facts (id, subject_entity_id, resident_uid, predicate, value,"
        " source) VALUES (?, ?, 'household', ?, ?, 'jellyfin')",
        (fid, subject, predicate, value),
    )


def _concept(conn, cid, ref_id, okf_path, embedding_id):
    conn.execute(
        "INSERT INTO concepts (id, ref_id, ref_kind, okf_path, embedding_id,"
        " content_hash) VALUES (?, ?, 'entity', ?, ?, 'h')",
        (cid, ref_id, okf_path, embedding_id),
    )


def _vector(conn, embedding_id, concept_id):
    conn.execute(
        "INSERT INTO okf_vectors (embedding_id, concept_id, model, dim, vector)"
        " VALUES (?, ?, 'nomic-embed-text', 1, ?)",
        (embedding_id, concept_id, b"\x00\x00\x00\x00"),
    )


@pytest.fixture
def legacy_env(tmp_path):
    """A vault + db as it looked BEFORE all Jellyfin types went projection-only:
    a song, its album, and its band each with markdown + concepts + okf_vectors,
    plus their facts (song on_album/by, band bio)."""
    db_path = str(tmp_path / "solaris.db")
    notes_dir = tmp_path / "notes"
    for domain in ("songs", "albums", "bands"):
        (notes_dir / "okf" / domain).mkdir(parents=True)
    song_md = notes_dir / "okf" / "songs" / "helter-skelter.md"
    album_md = notes_dir / "okf" / "albums" / "the-beatles-the-beatles.md"
    band_md = notes_dir / "okf" / "bands" / "the-beatles.md"
    song_md.write_text(
        "---\ntype: song\ntitle: Helter Skelter\n---\n", encoding="utf-8"
    )
    album_md.write_text("---\ntype: album\ntitle: The Beatles\n---\n", encoding="utf-8")
    band_md.write_text("---\ntype: band\ntitle: The Beatles\n---\n", encoding="utf-8")

    conn = sqlite3.connect(db_path)
    conn.executescript(_SCHEMA)
    _entity(conn, "song1", "song")
    _entity(conn, "album1", "album")
    _entity(conn, "band1", "band")
    _fact(conn, "f1", "song1", "on_album", "albums/the-beatles-the-beatles")
    _fact(conn, "f2", "song1", "by", "bands/the-beatles")
    _fact(conn, "f3", "band1", "bio", "An English rock band from Liverpool.")
    _concept(conn, "c-song", "song1", "okf/songs/helter-skelter.md", "e-song")
    _concept(
        conn, "c-album", "album1", "okf/albums/the-beatles-the-beatles.md", "e-album"
    )
    _concept(conn, "c-band", "band1", "okf/bands/the-beatles.md", "e-band")
    _vector(conn, "e-song", "song1")
    _vector(conn, "e-album", "album1")
    _vector(conn, "e-band", "band1")
    notes_index.ensure_schema(conn)
    notes_index.index_note(conn, notes_dir, "okf/songs/helter-skelter.md")
    conn.commit()
    conn.close()
    return db_path, notes_dir, song_md, album_md, band_md


def _counts(db_path):
    conn = sqlite3.connect(db_path)
    try:
        return {
            "concepts": conn.execute("SELECT COUNT(*) FROM concepts").fetchone()[0],
            "vectors": conn.execute("SELECT COUNT(*) FROM okf_vectors").fetchone()[0],
            "entities": conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0],
            "facts": conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0],
        }
    finally:
        conn.close()


def test_prune_removes_song_album_band_markdown_concepts_and_embeddings(legacy_env):
    db_path, notes_dir, song_md, album_md, band_md = legacy_env

    pruned = prune_legacy_music_artifacts(db_path, str(notes_dir))

    assert pruned == 3
    assert not song_md.exists() and not album_md.exists() and not band_md.exists()
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM concepts").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM okf_vectors").fetchone()[0] == 0
        # The FTS row for the pruned markdown is gone too.
        assert notes_index.search(conn, "helter") == []
    finally:
        conn.close()


def test_prune_keeps_entities_and_facts(legacy_env):
    db_path, notes_dir, *_ = legacy_env

    prune_legacy_music_artifacts(db_path, str(notes_dir))

    conn = sqlite3.connect(db_path)
    try:
        # All three entities survive ...
        assert conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0] == 3
        # ... with their facts, incl. the band's bio (the real content).
        preds = {r[0] for r in conn.execute("SELECT predicate FROM facts").fetchall()}
        assert preds == {"on_album", "by", "bio"}
        assert (
            conn.execute("SELECT value FROM facts WHERE predicate = 'bio'").fetchone()[
                0
            ]
            == "An English rock band from Liverpool."
        )
    finally:
        conn.close()


def test_prune_sweeps_orphaned_stub(legacy_env):
    """A markdown under a music domain dir with NO concepts row (a historical
    stub whose concept was dropped, or whose stored okf_path never matched the
    file) is swept — the type is projection-only, so no such markdown may remain.
    On the real library the concept-keyed pass alone left ~12k song stubs."""
    db_path, notes_dir, *_ = legacy_env
    orphan = notes_dir / "okf" / "albums" / "orphan-stub.md"
    orphan.write_text("---\ntype: album\ntitle: Orphan\n---\n", encoding="utf-8")
    conn = sqlite3.connect(db_path)
    notes_index.ensure_schema(conn)
    notes_index.index_note(conn, notes_dir, "okf/albums/orphan-stub.md")
    conn.commit()
    conn.close()

    # 3 concept-linked (song/album/band) + 1 swept orphan (no concept row).
    total = prune_legacy_music_artifacts(db_path, str(notes_dir))

    assert total == 4
    assert not orphan.exists()
    for domain in ("songs", "albums", "bands"):
        assert not list((notes_dir / "okf" / domain).glob("*.md"))  # dirs emptied
    conn = sqlite3.connect(db_path)
    try:
        assert notes_index.search(conn, "orphan") == []  # FTS row swept too
    finally:
        conn.close()

    # Idempotent: the emptied dirs yield nothing on a second pass.
    assert prune_legacy_music_artifacts(db_path, str(notes_dir)) == 0


def test_prune_is_idempotent(legacy_env):
    db_path, notes_dir, *_ = legacy_env

    assert prune_legacy_music_artifacts(db_path, str(notes_dir)) == 3
    after_first = _counts(db_path)

    # A pruned item now looks exactly like a projection-only one (no concepts
    # row), so the second pass matches nothing and mutates nothing.
    assert prune_legacy_music_artifacts(db_path, str(notes_dir)) == 0
    assert _counts(db_path) == after_first
    # Only the entities + facts remain — no markdown-backed rows survive.
    assert after_first == {"concepts": 0, "vectors": 0, "entities": 3, "facts": 3}

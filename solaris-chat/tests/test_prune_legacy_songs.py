"""Prune legacy per-song OKF artifacts (#878, ADR 0002/B7).

Before #877, a Jellyfin `song` also got an OKF markdown file, a `concepts` link
row, and an `okf_vectors` embedding. #878 prunes those pre-switch artifacts so a
legacy song matches a freshly projected one — entity + `on_album`/`by` facts
only. These tests prove: (1) the song markdown + `concepts` + `okf_vectors` rows
are gone while the entity + facts remain; (2) album/artist keep their markdown +
concepts + embedding; (3) a second run is a no-op (idempotent).

Schema is inlined DDL mirroring the #446/#877 migrations (importing alembic from
a chat test fails CI's clean env).
"""

from __future__ import annotations

import sqlite3

import pytest

from solaris_chat import notes_index
from solaris_chat.engine.ingest.prune import prune_legacy_song_artifacts

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
    """A vault + db as it looked BEFORE #877: a jellyfin song with markdown +
    concepts + okf_vectors + facts, plus an album and artist that keep theirs."""
    db_path = str(tmp_path / "solaris.db")
    notes_dir = tmp_path / "notes"
    (notes_dir / "okf" / "songs").mkdir(parents=True)
    (notes_dir / "okf" / "albums").mkdir(parents=True)
    (notes_dir / "okf" / "bands").mkdir(parents=True)
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


def test_prune_removes_song_markdown_concepts_and_embedding(legacy_env):
    db_path, notes_dir, song_md, _album, _band = legacy_env

    pruned = prune_legacy_song_artifacts(db_path, str(notes_dir))

    assert pruned == 1
    assert not song_md.exists()
    conn = sqlite3.connect(db_path)
    try:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM concepts WHERE ref_id = 'song1'"
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM okf_vectors WHERE embedding_id = 'e-song'"
            ).fetchone()[0]
            == 0
        )
        # The FTS row for the pruned markdown is gone too.
        assert notes_index.search(conn, "helter") == []
    finally:
        conn.close()


def test_prune_keeps_song_entity_and_facts(legacy_env):
    db_path, notes_dir, *_ = legacy_env

    prune_legacy_song_artifacts(db_path, str(notes_dir))

    conn = sqlite3.connect(db_path)
    try:
        assert (
            conn.execute("SELECT COUNT(*) FROM entities WHERE id = 'song1'").fetchone()[
                0
            ]
            == 1
        )
        preds = {
            r[0]
            for r in conn.execute(
                "SELECT predicate FROM facts WHERE subject_entity_id = 'song1'"
            ).fetchall()
        }
        assert preds == {"on_album", "by"}
    finally:
        conn.close()


def test_prune_leaves_album_and_artist_untouched(legacy_env):
    db_path, notes_dir, _song, album_md, band_md = legacy_env

    prune_legacy_song_artifacts(db_path, str(notes_dir))

    assert album_md.exists()
    assert band_md.exists()
    conn = sqlite3.connect(db_path)
    try:
        for ref in ("album1", "band1"):
            assert (
                conn.execute(
                    "SELECT COUNT(*) FROM concepts WHERE ref_id = ?", (ref,)
                ).fetchone()[0]
                == 1
            )
        for emb in ("e-album", "e-band"):
            assert (
                conn.execute(
                    "SELECT COUNT(*) FROM okf_vectors WHERE embedding_id = ?", (emb,)
                ).fetchone()[0]
                == 1
            )
    finally:
        conn.close()


def test_prune_sweeps_orphaned_song_markdown(legacy_env):
    """A song .md under okf/songs/ with NO concepts row (a historical stub whose
    concept was already dropped, or whose stored okf_path never matched the file)
    is swept — songs are projection-only, so no song markdown may remain. On the
    real library the concept-keyed pass alone left ~12k such stubs behind."""
    db_path, notes_dir, *_ = legacy_env
    orphan = notes_dir / "okf" / "songs" / "orphan-stub.md"
    orphan.write_text("---\ntype: song\ntitle: Orphan\n---\n", encoding="utf-8")
    conn = sqlite3.connect(db_path)
    notes_index.ensure_schema(conn)
    notes_index.index_note(conn, notes_dir, "okf/songs/orphan-stub.md")
    conn.commit()
    conn.close()

    # 1 concept-linked (helter-skelter) + 1 swept orphan (no concept row).
    total = prune_legacy_song_artifacts(db_path, str(notes_dir))

    assert total == 2
    assert not orphan.exists()
    assert not list((notes_dir / "okf" / "songs").glob("*.md"))  # dir emptied
    conn = sqlite3.connect(db_path)
    try:
        assert notes_index.search(conn, "orphan") == []  # FTS row swept too
        # album/artist markdown untouched by the sweep.
        assert (notes_dir / "okf" / "albums" / "the-beatles-the-beatles.md").exists()
    finally:
        conn.close()

    # Idempotent: the emptied dir yields nothing on a second pass.
    assert prune_legacy_song_artifacts(db_path, str(notes_dir)) == 0


def test_prune_is_idempotent(legacy_env):
    db_path, notes_dir, *_ = legacy_env

    assert prune_legacy_song_artifacts(db_path, str(notes_dir)) == 1
    after_first = _counts(db_path)

    # A pruned song now looks exactly like a projection-only one (no concepts row),
    # so the second pass matches nothing and mutates nothing.
    assert prune_legacy_song_artifacts(db_path, str(notes_dir)) == 0
    assert _counts(db_path) == after_first
    # Album + artist survived both passes.
    assert after_first == {"concepts": 2, "vectors": 2, "entities": 3, "facts": 2}

"""Empty-shell notes are rejected at write time and pruned from disk (#878).

The daily-chronicle template left untouched sections as `—`, and agent cron
sessions wrote title-only "Internal log: …" notes — both empty shells, not
knowledge. These tests prove: (1) `is_empty_note_shell` classifies them; (2)
`note_write` refuses to create one; (3) `prune_empty_note_shells` deletes the
ones already on disk (file + FTS + projection) while keeping real notes.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from solaris_chat import notes_index
from solaris_chat.engine.ingest.prune import prune_empty_note_shells
from solaris_chat.engine.knowledge import okf
from solaris_chat.engine.tools.notes import build_notes_tools

_EMPTY_JOURNAL = (
    "---\ntype: journal\ntags:\n  - solaris/journal\n---\n\n"
    "# Familienchronik — 2024-11-06\n\n"
    "## Höhepunkte des Tages\n—\n\n## Neue Notizen & Aufnahmen\n—\n\n"
    "## Haushalt & Ereignisse\n—\n\n## Persönliches & Stimmung\n—\n"
)
_TITLE_ONLY = "---\ntype: note\n---\n\n# Internal log: Date identification\n"
_FRONTMATTER_ONLY = "---\nentity_id:\n---\n"
_REAL = (
    "---\ntype: journal\n---\n\n# Familienchronik — 2024-11-07\n\n"
    "## Höhepunkte des Tages\nAnna hat laufen gelernt.\n"
)


@pytest.mark.parametrize(
    ("text", "empty"),
    [
        (_EMPTY_JOURNAL, True),
        (_TITLE_ONLY, True),
        (_FRONTMATTER_ONLY, True),
        ("", True),
        ("   \n\n", True),
        (_REAL, False),
        ("Milch kaufen.", False),
        ("---\ntype: note\n---\n\nEinkaufen: Milch, Brot.", False),
    ],
)
def test_is_empty_note_shell(text, empty):
    assert okf.is_empty_note_shell(text) is empty


def _write_handler(vault, uid="household"):
    for tool in build_notes_tools(vault, lambda: uid):
        if tool.name == "note_write":
            return tool.handler
    raise AssertionError("note_write tool not built")


async def test_note_write_rejects_empty_shell(tmp_path):
    write = _write_handler(str(tmp_path))
    out = json.loads(
        await write({"path": "journal/2024/2024-11-06.md", "content": _EMPTY_JOURNAL})
    )
    assert "error" in out
    assert not list(tmp_path.rglob("*.md"))


async def test_note_write_allows_real_note(tmp_path):
    write = _write_handler(str(tmp_path))
    out = json.loads(
        await write({"path": "idee.md", "content": "Wintergarten planen."})
    )
    assert out.get("written")
    assert list(tmp_path.rglob("*.md"))


_SCHEMA = """
CREATE TABLE entities (
  id TEXT PRIMARY KEY, type TEXT NOT NULL, canonical_name TEXT NOT NULL,
  resident_uid TEXT NOT NULL, source TEXT NOT NULL, content_hash TEXT NOT NULL,
  updated TEXT NOT NULL DEFAULT (datetime('now')));
CREATE TABLE facts (
  id TEXT PRIMARY KEY, subject_entity_id TEXT, resident_uid TEXT NOT NULL,
  predicate TEXT NOT NULL, value TEXT NOT NULL, confidence REAL,
  source TEXT NOT NULL, timestamp TEXT NOT NULL DEFAULT (datetime('now')));
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


def test_prune_empty_note_shells_deletes_shells_keeps_real(tmp_path):
    db_path = str(tmp_path / "solaris.db")
    notes_dir = tmp_path / "notes"
    (notes_dir / "journal" / "2024").mkdir(parents=True)
    (notes_dir / "okf" / "notes").mkdir(parents=True)
    empty_j = notes_dir / "journal" / "2024" / "2024-11-06.md"
    real_j = notes_dir / "journal" / "2024" / "2024-11-07.md"
    empty_n = notes_dir / "okf" / "notes" / "internal-log.md"
    real_n = notes_dir / "okf" / "notes" / "familienchronik.md"
    empty_j.write_text(_EMPTY_JOURNAL, encoding="utf-8")
    real_j.write_text(_REAL, encoding="utf-8")
    empty_n.write_text(_TITLE_ONLY, encoding="utf-8")
    real_n.write_text(
        "---\ntype: note\n---\n\nEcht wichtiger Inhalt hier.", encoding="utf-8"
    )

    conn = sqlite3.connect(db_path)
    conn.executescript(_SCHEMA)
    # The empty note carries a full projection (entity + fact + concept + vector)
    # that must be deleted along with the file.
    conn.execute(
        "INSERT INTO entities (id, type, canonical_name, resident_uid, source,"
        " content_hash) VALUES ('n1', 'note', 'Internal log', 'household',"
        " 'obsidian', 'h')"
    )
    conn.execute(
        "INSERT INTO facts (id, subject_entity_id, resident_uid, predicate, value,"
        " source) VALUES ('fx', 'n1', 'household', 'about', 'x', 'obsidian')"
    )
    conn.execute(
        "INSERT INTO concepts (id, ref_id, ref_kind, okf_path, embedding_id,"
        " content_hash) VALUES ('c1', 'n1', 'entity', 'okf/notes/internal-log.md',"
        " 'e1', 'h')"
    )
    conn.execute(
        "INSERT INTO okf_vectors (embedding_id, concept_id, model, dim, vector)"
        " VALUES ('e1', 'n1', 'm', 1, X'00')"
    )
    notes_index.ensure_schema(conn)
    notes_index.index_note(conn, notes_dir, "okf/notes/internal-log.md")
    conn.commit()
    conn.close()

    pruned = prune_empty_note_shells(db_path, str(notes_dir))

    assert pruned == 2  # empty journal + empty note
    assert not empty_j.exists() and not empty_n.exists()
    assert real_j.exists() and real_n.exists()
    conn = sqlite3.connect(db_path)
    try:
        for table in ("concepts", "okf_vectors", "entities", "facts"):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0  # noqa: S608
        assert notes_index.search(conn, "identification") == []  # FTS swept
    finally:
        conn.close()

    # Idempotent: nothing empty remains.
    assert prune_empty_note_shells(db_path, str(notes_dir)) == 0

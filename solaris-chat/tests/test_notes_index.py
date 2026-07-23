"""FTS5 notes-index build, incremental update, and full-vault query (#830).

The FTS schema is owned by the alembic migration in `database/`; importing
alembic from a solaris-chat test fails CI's clean env, so these build the index
from `notes_index.ensure_schema` (the same DDL, mirrored in-module).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from solaris_chat import notes_index


def _note(root: Path, rel: str, body: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    notes_index.ensure_schema(conn)
    return conn


def test_backfill_indexes_and_query_finds_by_keyword(tmp_path):
    root = tmp_path / "notes"
    _note(root, "okf/events/2024-05-01-wein.md", "# Weinfest\n\nRotwein im Keller.\n")
    _note(root, "loose.md", "# Loose\n\nNichts hier.\n")
    db = str(tmp_path / "solaris.db")
    changed = notes_index.backfill(db, str(root))
    assert changed == 2

    conn = sqlite3.connect(db)
    try:
        assert notes_index.search(conn, "rotwein") == ["okf/events/2024-05-01-wein.md"]
        assert notes_index.search(conn, "loose") == ["loose.md"]
        assert notes_index.search(conn, "nichtvorhanden") == []
    finally:
        conn.close()


def test_frontmatter_is_searchable(tmp_path):
    root = tmp_path / "notes"
    _note(
        root,
        "okf/bands/beatles.md",
        "---\ntype: band\ntitle: The Beatles\n---\n\n# The Beatles\n\nRock.\n",
    )
    db = str(tmp_path / "solaris.db")
    notes_index.backfill(db, str(root))
    conn = sqlite3.connect(db)
    try:
        # a frontmatter-only term (the `type: band` line) still surfaces the note
        assert notes_index.search(conn, "band") == ["okf/bands/beatles.md"]
    finally:
        conn.close()


def test_incremental_skips_unchanged_note(tmp_path):
    root = tmp_path / "notes"
    rel = "okf/events/2024-05-01-wein.md"
    _note(root, rel, "# Weinfest\n\nRotwein.\n")
    conn = _conn()
    assert notes_index.index_note(conn, root, rel) is True
    # Same content, unchanged hash → a no-op skip (idempotent re-ingest).
    assert notes_index.index_note(conn, root, rel) is False
    # Exactly one row — no accretion of stale duplicates.
    assert conn.execute("SELECT COUNT(*) FROM fts_notes").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM fts_notes_meta").fetchone()[0] == 1


def test_incremental_reindexes_changed_note(tmp_path):
    root = tmp_path / "notes"
    rel = "note.md"
    _note(root, rel, "# One\n\nalpha\n")
    conn = _conn()
    notes_index.index_note(conn, root, rel)
    assert notes_index.search(conn, "alpha") == [rel]

    _note(root, rel, "# One\n\nbeta\n")
    assert notes_index.index_note(conn, root, rel) is True
    assert notes_index.search(conn, "alpha") == []  # stale term gone
    assert notes_index.search(conn, "beta") == [rel]
    assert conn.execute("SELECT COUNT(*) FROM fts_notes").fetchone()[0] == 1


def test_deleted_file_drops_from_index(tmp_path):
    root = tmp_path / "notes"
    rel = "note.md"
    _note(root, rel, "# One\n\nalpha\n")
    conn = _conn()
    notes_index.index_note(conn, root, rel)
    (root / rel).unlink()
    assert notes_index.index_note(conn, root, rel) is True
    assert notes_index.search(conn, "alpha") == []
    assert conn.execute("SELECT COUNT(*) FROM fts_notes_meta").fetchone()[0] == 0


def test_backfill_covers_note_beyond_the_20k_walk_budget(tmp_path):
    """The point of #830: a note the walk-budget can't reach is still indexed.

    `iter_vault_md`'s default 20k budget stops early on a huge flat directory; the
    FTS backfill uses no budget, so a note past that cutoff is searchable."""
    root = tmp_path / "notes"
    events = root / "okf" / "events"
    events.mkdir(parents=True)
    # Sorted-order filler so the target sorts LAST — a budgeted walk never reaches
    # it, an unbudgeted backfill does. Kept small so the test stays fast; the
    # budget is lowered to make the same ordering effect deterministic.
    for i in range(30):
        (events / f"a{i:03d}.md").write_text(f"# filler {i}\n", encoding="utf-8")
    target = events / "zzz-target.md"
    target.write_text("# Ziel\n\neinzigartigwort\n", encoding="utf-8")

    from solaris_chat import notes_search

    walked = list(notes_search.iter_vault_md(root, budget=10))
    assert target not in walked  # the budgeted walk can't reach it

    db = str(tmp_path / "solaris.db")
    notes_index.backfill(db, str(root))
    conn = sqlite3.connect(db)
    try:
        assert notes_index.search(conn, "einzigartigwort") == [
            "okf/events/zzz-target.md"
        ]
    finally:
        conn.close()


def test_backfill_excludes_upload_companion(tmp_path):
    """A backfill indexes the derived OKF note but NOT the upload companion, so
    `.note` search returns one hit, not the pre-#998 duplicate."""
    root = tmp_path / "notes"
    _note(root, "users/mdopp/uploads/scan.md", "# Scan\n\neinzigartigwort\n")
    _note(root, "okf/notes/scan.md", "# Scan\n\neinzigartigwort\n")
    db = str(tmp_path / "solaris.db")
    notes_index.backfill(db, str(root))
    conn = sqlite3.connect(db)
    try:
        assert notes_index.search(conn, "einzigartigwort") == ["okf/notes/scan.md"]
    finally:
        conn.close()


def test_backfill_sweeps_pre_existing_companion_row(tmp_path):
    """A companion indexed by a pre-#998 backfill is swept on the next pass, so an
    already-duplicated `.note` search self-heals to one hit."""
    root = tmp_path / "notes"
    db = str(tmp_path / "solaris.db")
    _note(root, "users/mdopp/uploads/scan.md", "# Scan\n\neinzigartigwort\n")
    # Simulate the pre-#998 index state: the companion is already in the table.
    conn = sqlite3.connect(db)
    notes_index.ensure_schema(conn)
    notes_index.index_note(conn, root, "users/mdopp/uploads/scan.md")
    conn.commit()
    assert notes_index.search(conn, "einzigartigwort") == [
        "users/mdopp/uploads/scan.md"
    ]
    conn.close()

    _note(root, "okf/notes/scan.md", "# Scan\n\neinzigartigwort\n")
    notes_index.backfill(db, str(root))
    conn = sqlite3.connect(db)
    try:
        assert notes_index.search(conn, "einzigartigwort") == ["okf/notes/scan.md"]
    finally:
        conn.close()


def test_search_ignores_fts_operator_tokens(tmp_path):
    """A raw query with FTS operators/punctuation must not raise a syntax error."""
    root = tmp_path / "notes"
    _note(root, "note.md", "# Wein\n\nWein und Käse.\n")
    conn = _conn()
    notes_index.index_note(conn, root, "note.md")
    # `AND`/`&`/`*` would be FTS syntax; tokenized+quoted, this is safe recall.
    assert notes_index.search(conn, "wein AND käse *") == ["note.md"]
    assert notes_index.search(conn, "") == []

"""SQLite-FTS5 full-text index over the notes vault (#830).

`notes_search` used to keyword-search by walking `iter_vault_md`, whose 20k-file
budget left ~80% of the ~99k-file vault (the flat `okf/events/` immich backfill)
invisible. This module is the index it queries instead: an FTS5 table over every
note's path + frontmatter + content, covering 100% of the vault in one indexed
query — no walk budget.

The index lives in the engine `.db` (the `fts_notes` / `fts_notes_meta` tables
from migration 0024). It is a *rebuildable projection* of the vault (the source
of truth): drop the tables and `backfill` refills them.

  - `backfill(db_path, notes_dir)` — boot-time full-vault (re)index, streamed
    lazily over the whole vault (no budget, no OOM on 99k files), content-hash
    gated so a re-run only touches changed notes and logs progress.
  - `index_note(conn, root, path)` — the incremental per-note upsert every
    ingest write calls; a no-op when the note's content_hash is unchanged.
  - `search(conn, query, ...)` — the FTS MATCH query `notes_search` runs to get
    candidate vault-relative paths.

FTS5 is built into CPython's sqlite3, so no extension load is needed.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from pathlib import Path

from solaris_chat.logging import log
from solaris_chat.notes_search import iter_vault_md

_MAX_BYTES = 256 * 1024  # skip pathological files; matches notes_search readers

# The index tables — kept as a local DDL string (NOT imported from alembic, which
# only ships in database/; a chat test importing it fails CI's clean env). The
# migration owns these on the box; this mirror lets tests build the index in a
# throwaway :memory: db. `IF NOT EXISTS` keeps it a no-op against a migrated db.
_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS fts_notes USING fts5 (
  path,
  frontmatter,
  content
);
CREATE TABLE IF NOT EXISTS fts_notes_meta (
  path         TEXT PRIMARY KEY,
  content_hash TEXT NOT NULL,
  rowid_ref    INTEGER NOT NULL
);
"""

# FTS5 query syntax reserves these; a raw user query token containing one (or a
# bare operator) is a syntax error. We quote every token as a phrase and OR them,
# so "wein & käse" becomes `"wein" OR "käse"` — a safe candidate recall query.
_TOKEN_RE = re.compile(r"\w+", re.UNICODE)


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the FTS tables if absent (no-op on a migrated box db)."""
    conn.executescript(_SCHEMA)


def _split(text: str) -> tuple[str, str]:
    """Split a note into (frontmatter, content) on the leading `---` fence.

    A note with no frontmatter fence is all content. Both halves are indexed so a
    `type:`/`tags:`/`title:` frontmatter term is still findable."""
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            fm = text[: end + 4]
            return fm, text[end + 4 :]
    return "", text


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _delete_row(conn: sqlite3.Connection, rel: str) -> None:
    row = conn.execute(
        "SELECT rowid_ref FROM fts_notes_meta WHERE path = ?", (rel,)
    ).fetchone()
    if row is None:
        return
    conn.execute("DELETE FROM fts_notes WHERE rowid = ?", (row[0],))
    conn.execute("DELETE FROM fts_notes_meta WHERE path = ?", (rel,))


def index_note(conn: sqlite3.Connection, root: Path, rel: str) -> bool:
    """(Re)index one vault-relative note; True when it changed, False when skipped.

    Content-hash gated + idempotent: an unchanged note is a no-op, a changed one
    is re-indexed in place (delete + reinsert, so FTS can't accrete stale rows).
    A missing/unreadable/oversize file is dropped from the index. The caller
    commits."""
    ensure_schema(conn)
    path = (root / rel).resolve()
    try:
        if not path.is_file() or path.stat().st_size > _MAX_BYTES:
            _delete_row(conn, rel)
            return True
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        _delete_row(conn, rel)
        return True
    new_hash = _content_hash(text)
    row = conn.execute(
        "SELECT content_hash FROM fts_notes_meta WHERE path = ?", (rel,)
    ).fetchone()
    if row is not None and row[0] == new_hash:
        return False
    _delete_row(conn, rel)
    frontmatter, content = _split(text)
    cur = conn.execute(
        "INSERT INTO fts_notes (path, frontmatter, content) VALUES (?, ?, ?)",
        (rel, frontmatter, content),
    )
    conn.execute(
        "INSERT INTO fts_notes_meta (path, content_hash, rowid_ref) VALUES (?, ?, ?)",
        (rel, new_hash, cur.lastrowid),
    )
    return True


def backfill(db_path: str, notes_dir: str) -> int:
    """(Re)index the whole vault into the FTS tables; return the changed count.

    Boot-time full-vault pass — streams `iter_vault_md` with NO walk budget, so
    it covers all ~99k files (the point of #830) and never materializes the file
    list. Content-hash gated (a re-boot only touches changed notes) and logs
    progress every few thousand files so the box shows it running, not hung."""
    root = Path(notes_dir)
    if not root.is_dir():
        return 0
    conn = sqlite3.connect(db_path, timeout=30)
    conn.execute("PRAGMA busy_timeout = 10000")
    try:
        ensure_schema(conn)
        seen = 0
        changed = 0
        # No budget: the whole vault. iter_vault_md(budget=None) still prunes the
        # Syncthing history/dot subtrees, so it can't runaway on `.stversions/`.
        for path in iter_vault_md(root, budget=None):
            rel = str(path.relative_to(root))
            if index_note(conn, root, rel):
                changed += 1
            seen += 1
            if seen % 5000 == 0:
                conn.commit()
                log.info(
                    "engine.notes_index.backfill_progress", seen=seen, changed=changed
                )
        conn.commit()
        log.info("engine.notes_index.backfill_done", seen=seen, changed=changed)
        return changed
    finally:
        conn.close()


def _match_query(query: str) -> str:
    """A safe FTS5 MATCH string: each query token as an OR'd quoted phrase."""
    toks = _TOKEN_RE.findall(query.lower())
    return " OR ".join(f'"{t}"' for t in toks)


def search(conn: sqlite3.Connection, query: str, limit: int = 200) -> list[str]:
    """Vault-relative paths matching `query`, best FTS rank first, capped.

    OR-recall over the tokens (a candidate set for the caller's own scoring/
    scope pass), so a note carrying any query term surfaces. Empty when the
    query has no word tokens or nothing matches."""
    match = _match_query(query)
    if not match:
        return []
    ensure_schema(conn)
    rows = conn.execute(
        "SELECT path FROM fts_notes WHERE fts_notes MATCH ? ORDER BY rank LIMIT ?",
        (match, limit),
    ).fetchall()
    return [r[0] for r in rows]

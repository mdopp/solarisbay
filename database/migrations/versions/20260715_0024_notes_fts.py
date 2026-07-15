"""add notes FTS5 index over the vault (rebuildable)

Revision ID: 0024_notes_fts
Revises: 0023_wartung_seen_approvals
Create Date: 2026-07-15

The full-text index `notes_search` queries instead of walking the ~99k-file
vault (#830). `iter_vault_md`'s 20k-file walk budget left ~80% of the vault
(the flat `okf/events/` immich backfill) invisible to keyword search; an FTS5
table covers 100% of it in one indexed query. Derived + rebuildable: drop the
tables and the boot backfill refills them from the vault (the source of truth).

`fts_notes` is a contentless FTS5 table (path + frontmatter + content columns);
`fts_notes_meta(path, content_hash)` is the side table that makes the
incremental per-note update content-hash gated and idempotent — an unchanged
note is skipped, a changed one re-indexed, both keyed by vault-relative path.
"""

from __future__ import annotations

from alembic import op


revision = "0024_notes_fts"
down_revision = "0023_wartung_seen_approvals"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS fts_notes USING fts5 (
          path,
          frontmatter,
          content
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS fts_notes_meta (
          path         TEXT PRIMARY KEY,
          content_hash TEXT NOT NULL,
          rowid_ref    INTEGER NOT NULL
        )
        """
    )


def downgrade() -> None:
    raise NotImplementedError(
        "Downgrade is not supported. Delete solaris.db and re-run upgrade if needed."
    )

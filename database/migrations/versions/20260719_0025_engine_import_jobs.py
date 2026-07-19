"""add engine_import_jobs table for durable import-job persistence

Revision ID: 0025_engine_import_jobs
Revises: 0024_notes_fts
Create Date: 2026-07-19

S3 of the generic-Import epic (#864). A row per import job (Google-Takeout &
future sources): the durable store the in-engine job runner enqueues to,
dispatches from and resumes at boot — replacing the standalone tool's
per-job JSON files. Modelled on `engine_timers` (0009): owner-scoped, a
`status` state machine (pending/running/done/failed/interrupted) and a
`(status)` index so the boot resume-scan finds unfinished rows without a full
scan. `payload`/`progress` ride JSON-serialised TEXT columns (the runner
input + last snapshot), so a resumed job re-attaches to its progress.
"""

from __future__ import annotations

from alembic import op


revision = "0025_engine_import_jobs"
down_revision = "0024_notes_fts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS engine_import_jobs (
          id         TEXT PRIMARY KEY,
          owner_uid  TEXT NOT NULL,
          kind       TEXT NOT NULL,
          status     TEXT NOT NULL DEFAULT 'pending',
          payload    TEXT NOT NULL DEFAULT '{}',
          progress   TEXT NOT NULL DEFAULT '{}',
          error      TEXT,
          created_at TEXT NOT NULL DEFAULT (datetime('now')),
          updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS engine_import_jobs_status_idx "
        "ON engine_import_jobs (status)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS engine_import_jobs_owner_idx "
        "ON engine_import_jobs (owner_uid, created_at)"
    )


def downgrade() -> None:
    raise NotImplementedError(
        "Downgrade is not supported. Delete solaris.db and re-run upgrade if needed."
    )

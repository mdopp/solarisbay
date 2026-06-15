"""add engine_cron_runs — durable last-run stamps for the engine night jobs

Revision ID: 0010_engine_cron_runs
Revises: 0009_engine_sessions
Create Date: 2026-06-12

Phase 3 of the Solaris Engine: the night jobs (daily-chronicle,
problem-summarizer, chat-compactor) move from Hermes' jobs.json onto the
engine scheduler. Jobs are defined in code (idempotent by construction —
the Hermes-era upgrade de-dup problem from #332 can't recur); this table
only remembers each job's last fired slot so a restart inside the same
cron minute doesn't double-run it.
"""

from __future__ import annotations

from alembic import op

revision = "0010_engine_cron_runs"
down_revision = "0009_engine_sessions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS engine_cron_runs (
          name     TEXT PRIMARY KEY,
          last_run TEXT NOT NULL
        )
        """
    )


def downgrade() -> None:
    raise NotImplementedError(
        "Downgrade is not supported. Delete solaris.db and re-run upgrade if needed."
    )

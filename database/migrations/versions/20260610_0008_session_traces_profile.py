"""add profile column to session_traces

Revision ID: 0008_session_traces_profile
Revises: 0007_session_traces
Create Date: 2026-06-10

Tag each persisted LLM step with the Hermes profile that produced it. The trace
proxy reads the `Active Hermes profile: <name>.` line Hermes injects into the
system prompt, so the steps panel can show whether a turn was served by the
household or admin profile (was previously ambiguous). Nullable — pre-existing
rows and older-Hermes calls (no profile line) stay NULL.
"""

from __future__ import annotations

from alembic import op


revision = "0008_session_traces_profile"
down_revision = "0007_session_traces"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE session_traces ADD COLUMN profile TEXT")


def downgrade() -> None:
    # One-way, matching the other migrations: a downgrade would drop the column.
    raise NotImplementedError(
        "Downgrade is not supported. Delete solaris.db and re-run upgrade if needed."
    )

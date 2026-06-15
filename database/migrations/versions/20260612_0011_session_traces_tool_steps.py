"""add step_kind + tool_name to session_traces

Revision ID: 0011_session_traces_tool_steps
Revises: 0010_engine_cron_runs
Create Date: 2026-06-12

A turn's trace is now the full interleaved step list — LLM calls AND tool
executions (#346). `step_kind` distinguishes them ('llm'|'tool'); `tool_name`
carries the dispatched tool for a tool step (NULL for an LLM step). Both
nullable so pre-existing rows stay valid; a read treats a missing `step_kind`
as the legacy 'llm'.
"""

from __future__ import annotations

from alembic import op


revision = "0011_session_traces_tool_steps"
down_revision = "0010_engine_cron_runs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE session_traces ADD COLUMN step_kind TEXT")
    op.execute("ALTER TABLE session_traces ADD COLUMN tool_name TEXT")


def downgrade() -> None:
    # One-way, matching the other migrations: a downgrade would drop the columns.
    raise NotImplementedError(
        "Downgrade is not supported. Delete solaris.db and re-run upgrade if needed."
    )

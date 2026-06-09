"""add session_traces per-message LLM trace table

Revision ID: 0007_session_traces
Revises: 0006_mentions
Create Date: 2026-06-09

Phase 2 of the LLM trace sidecar (#306). Each chat turn carries a stable
`trace_id`; all of that turn's Ollama calls (captured by the trace proxy) are
persisted here in order so reopening a chat shows the same trace. A row is one
LLM step keyed by (session_id, trace_id, step_order); `detail_id` links back to
the proxy's `/__traces__/<id>` exact-content record. Scoped per-resident by
`owner_uid` (D3, like topics/session_topics/mentions).
"""

from __future__ import annotations

from alembic import op


revision = "0007_session_traces"
down_revision = "0006_mentions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS session_traces (
          session_id        TEXT NOT NULL,
          trace_id          TEXT NOT NULL,
          step_order        INTEGER NOT NULL,
          owner_uid         TEXT NOT NULL,
          model             TEXT,
          wall_s            REAL,
          prompt_tokens     INTEGER,
          completion_tokens INTEGER,
          context_free      INTEGER,
          finish_reason     TEXT,
          n_tools           INTEGER,
          detail_id         INTEGER,
          created_at        TEXT NOT NULL DEFAULT (datetime('now')),
          PRIMARY KEY (session_id, trace_id, step_order)
        )
        """
    )
    # The trace panel reads a session's steps in turn-then-step order.
    op.execute(
        "CREATE INDEX IF NOT EXISTS session_traces_session_idx "
        "ON session_traces (session_id, owner_uid)"
    )


def downgrade() -> None:
    # One-way, matching the other migrations: a downgrade would drop every
    # persisted trace.
    raise NotImplementedError(
        "Downgrade is not supported. Delete solilos.db and re-run upgrade if needed."
    )

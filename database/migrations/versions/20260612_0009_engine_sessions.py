"""add Solaris Engine session, message and timer tables

Revision ID: 0009_engine_sessions
Revises: 0008_session_traces_profile
Create Date: 2026-06-12

Phase 1 of the Solaris Engine (Hermes replacement): conversation state moves from
Hermes' per-gateway session store into solaris.db, owned by the chat server's
in-process engine. `engine_sessions` carries ownership as a plain column —
the Hermes-era `[uid:...]` title-marker workaround dies with the external
store. `engine_messages` is the verbatim turn history (tool calls and
reasoning ride JSON columns so a reopened chat can re-render exactly).
`engine_timers` backs the timer/alarm/reminder tool: rows survive a restart,
the scheduler re-arms pending ones at boot.
"""

from __future__ import annotations

from alembic import op

revision = "0009_engine_sessions"
down_revision = "0008_session_traces_profile"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS engine_sessions (
          id            TEXT PRIMARY KEY,
          owner_uid     TEXT NOT NULL,
          title         TEXT NOT NULL DEFAULT '',
          profile       TEXT NOT NULL DEFAULT 'household',
          system_prompt TEXT NOT NULL DEFAULT '',
          ephemeral     INTEGER NOT NULL DEFAULT 0,
          maintenance   INTEGER NOT NULL DEFAULT 0,
          input_tokens  INTEGER NOT NULL DEFAULT 0,
          output_tokens INTEGER NOT NULL DEFAULT 0,
          created_at    TEXT NOT NULL DEFAULT (datetime('now')),
          last_activity TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS engine_sessions_owner_idx "
        "ON engine_sessions (owner_uid, last_activity)"
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS engine_messages (
          session_id  TEXT NOT NULL,
          seq         INTEGER NOT NULL,
          role        TEXT NOT NULL,
          content     TEXT NOT NULL DEFAULT '',
          reasoning   TEXT,
          tool_calls  TEXT,
          images      TEXT,
          created_at  TEXT NOT NULL DEFAULT (datetime('now')),
          PRIMARY KEY (session_id, seq)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS engine_timers (
          id         TEXT PRIMARY KEY,
          owner_uid  TEXT NOT NULL,
          kind       TEXT NOT NULL DEFAULT 'timer',
          label      TEXT NOT NULL DEFAULT '',
          fire_at    TEXT NOT NULL,
          rrule      TEXT,
          session_id TEXT,
          status     TEXT NOT NULL DEFAULT 'pending',
          created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS engine_timers_pending_idx "
        "ON engine_timers (status, fire_at)"
    )


def downgrade() -> None:
    # One-way, matching the other migrations: a downgrade would drop live
    # conversation history.
    raise NotImplementedError(
        "Downgrade is not supported. Delete solaris.db and re-run upgrade if needed."
    )

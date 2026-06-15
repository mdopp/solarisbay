"""add voice_uid_stash table

Revision ID: 0012_voice_uid_stash
Revises: 0011_session_traces_tool_steps
Create Date: 2026-06-13

The transcript-keyed uid side-channel for the live HA Assist path (#350,
approach b). When the gatekeeper serves as HA's Wyoming STT provider it
resolves the speaking resident (ECAPA + k-NN) and stashes
`{transcript -> uid}` here; the engine facade looks the uid up by the
incoming utterance text when HA calls `conversation.solaris` for the same turn.
Consume-once + short TTL so a stale uid never leaks into a later turn.
"""

from __future__ import annotations

from alembic import op


revision = "0012_voice_uid_stash"
down_revision = "0011_session_traces_tool_steps"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE voice_uid_stash (
            transcript TEXT PRIMARY KEY,
            uid        TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )


def downgrade() -> None:
    raise NotImplementedError(
        "Downgrade is not supported. Delete solaris.db and re-run upgrade if needed."
    )

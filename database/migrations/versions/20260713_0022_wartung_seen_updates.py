"""add wartung_seen_updates table (Wartung update-card dedupe, #788)

Revision ID: 0022_wartung_seen_updates
Revises: 0021_device_tokens
Create Date: 2026-07-13

The dedupe ledger behind the Wartung update-notification cards (#788): the
UpdatePoller cards a pending ServiceBay image/template update exactly once, so it
records each carded update's identity here (`image:<service>:<digest>` /
`template:<name>:<version>`). A restart then re-reads what it already announced
instead of re-carding every pending update on the next tick. `updates.mark_seen`
degrades to "treat as new" when this table is missing, so the engine is safe to
deploy before the migration lands (it just re-cards until it does).
"""

from __future__ import annotations

from alembic import op


revision = "0022_wartung_seen_updates"
down_revision = "0021_device_tokens"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS wartung_seen_updates (
          update_id TEXT PRIMARY KEY,
          seen_at   TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )


def downgrade() -> None:
    raise NotImplementedError(
        "Downgrade is not supported. Delete solaris.db and re-run upgrade if needed."
    )

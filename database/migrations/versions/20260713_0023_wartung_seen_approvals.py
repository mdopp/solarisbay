"""add wartung_seen_approvals table (Wartung approval-card dedupe, #790)

Revision ID: 0023_wartung_seen_approvals
Revises: 0022_wartung_seen_updates
Create Date: 2026-07-13

The dedupe ledger behind the Wartung incoming-approval cards (#790): the
ApprovalPoller cards a pending ServiceBay approval request exactly once, so it
records each carded request's id here. A restart then re-reads what it already
announced instead of re-carding every pending approval on the next tick.
`approvals.mark_seen` degrades to "treat as new" when this table is missing, so
the engine is safe to deploy before the migration lands (it just re-cards until
it does). Mirrors 0022 (#788) — this revision chains on it because the approval
cards reuse the same Wartung injection path the update cards introduced.
"""

from __future__ import annotations

from alembic import op


revision = "0023_wartung_seen_approvals"
down_revision = "0022_wartung_seen_updates"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS wartung_seen_approvals (
          approval_id TEXT PRIMARY KEY,
          seen_at     TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )


def downgrade() -> None:
    raise NotImplementedError(
        "Downgrade is not supported. Delete solaris.db and re-run upgrade if needed."
    )

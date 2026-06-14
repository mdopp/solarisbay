"""add pending_residents table

Revision ID: 0013_pending_residents
Revises: 0012_voice_uid_stash
Create Date: 2026-06-13

The local record a guest's registration request lands in once the onboarding
flow (#376) has enrolled their voice: a candidate uid + display name, when it
was requested, whether the gatekeeper enrolment succeeded, and a `pending`
status that the admin-approval step (#355, separate) flips to approved/denied.
This is the artifact #355 reads from — no account exists until an admin acts.
Scoped to nothing: a pending request belongs to the household, not a resident
(the candidate has no uid of their own yet beyond the one they asked for).
"""

from __future__ import annotations

from alembic import op


revision = "0013_pending_residents"
down_revision = "0012_voice_uid_stash"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE pending_residents (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            uid          TEXT NOT NULL,
            display_name TEXT NOT NULL,
            status       TEXT NOT NULL DEFAULT 'pending',
            enrolled     INTEGER NOT NULL DEFAULT 0,
            requested_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )


def downgrade() -> None:
    raise NotImplementedError(
        "Downgrade is not supported. Delete solaris.db and re-run upgrade if needed."
    )

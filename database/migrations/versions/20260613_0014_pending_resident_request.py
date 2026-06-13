"""add request_id + email to pending_residents

Revision ID: 0014_pending_resident_request
Revises: 0013_pending_residents
Create Date: 2026-06-13

The admin-approval step (#355) files the pending request onto ServiceBay's
central access-request list and gets back a request id; it polls that id to
learn when the admin has resolved the request. We persist the id on the
pending_residents row so a later poll (a different turn/process) can find it,
and the candidate's email so the filing can feed SB's LLDAP user on approval.
Both are nullable: rows written before #355 (or by a flow that has no email)
simply carry NULL.
"""

from __future__ import annotations

from alembic import op


revision = "0014_pending_resident_request"
down_revision = "0013_pending_residents"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE pending_residents ADD COLUMN request_id TEXT")
    op.execute("ALTER TABLE pending_residents ADD COLUMN email TEXT")


def downgrade() -> None:
    raise NotImplementedError(
        "Downgrade is not supported. Delete solilos.db and re-run upgrade if needed."
    )

"""add token counts to cloud_audit

Revision ID: 0002_add_token_counts
Revises: 0001_baseline
Create Date: 2026-05-25

Adds prompt_tokens, completion_tokens, and total_tokens to the cloud_audit table
to allow direct SQL-native analytical insights on LLM and cloud token usage.
"""

from __future__ import annotations

from alembic import op


revision = "0002_add_token_counts"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add columns to cloud_audit.
    # SQLite does not support adding multiple columns in one ALTER TABLE,
    # so we execute them as three separate ALTER TABLE statements.
    op.execute("ALTER TABLE cloud_audit ADD COLUMN prompt_tokens INTEGER")
    op.execute("ALTER TABLE cloud_audit ADD COLUMN completion_tokens INTEGER")
    op.execute("ALTER TABLE cloud_audit ADD COLUMN total_tokens INTEGER")


def downgrade() -> None:
    # SQLite does not support dropping columns easily, raising NotImplementedError
    # or using a temporary table would be needed. Since this is a lightweight schema,
    # raising NotImplementedError matches the baseline's pattern.
    raise NotImplementedError(
        "Downgrade is not supported. Delete solaris.db and re-run upgrade if needed."
    )

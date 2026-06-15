"""add session_topics chat<->topic assignment table

Revision ID: 0005_session_topics
Revises: 0004_topics
Create Date: 2026-06-08

The chat<->topic assignment (solaris-architecture.md §3, D1): a chat has exactly
one *primary* topic and any number of *secondary* tags. `topic_slug` references
the `topics` registry (0004); `owner_uid` scopes the assignment per-resident
(D3). The partial unique index enforces at-most-one primary row per session;
the (session_id, topic_slug) PK keeps a topic from being assigned twice to the
same chat.
"""

from __future__ import annotations

from alembic import op


revision = "0005_session_topics"
down_revision = "0004_topics"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS session_topics (
          session_id  TEXT NOT NULL,
          topic_slug  TEXT NOT NULL REFERENCES topics(slug),
          role        TEXT NOT NULL DEFAULT 'secondary',
          owner_uid   TEXT NOT NULL,
          created_at  TEXT NOT NULL DEFAULT (datetime('now')),
          PRIMARY KEY (session_id, topic_slug)
        )
        """
    )
    # At most one primary topic per chat (D1). Partial index so secondary rows
    # are unconstrained.
    op.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS session_topics_one_primary_idx
          ON session_topics (session_id)
          WHERE role = 'primary'
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS session_topics_session_idx "
        "ON session_topics (session_id)"
    )


def downgrade() -> None:
    # One-way, matching the other migrations: a downgrade would drop every
    # chat<->topic assignment.
    raise NotImplementedError(
        "Downgrade is not supported. Delete solaris.db and re-run upgrade if needed."
    )

"""add inline mentions table for #tag / @person

Revision ID: 0006_mentions
Revises: 0005_session_topics
Create Date: 2026-06-08

Inline mentions (#279, solaris-architecture.md §3 "Mention-based tagging"): the
`#tag` / `@person` tokens typed in a chat replace the retired Thema picker. Each
mention is keyed by `session_id` + a per-session message reference
(`message_ref`, the user-turn ordinal that carried it — drives jump-to-message)
+ `kind` ('tag'|'person') + `value` (the tag/person string), scoped per-resident
by `owner_uid` (D3, like topics/session_topics). The PK keeps the same
(value, kind) from being recorded twice for the same message.
"""

from __future__ import annotations

from alembic import op


revision = "0006_mentions"
down_revision = "0005_session_topics"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS mentions (
          session_id  TEXT NOT NULL,
          message_ref INTEGER NOT NULL,
          kind        TEXT NOT NULL CHECK (kind IN ('tag', 'person')),
          value       TEXT NOT NULL,
          owner_uid   TEXT NOT NULL,
          created_at  TEXT NOT NULL DEFAULT (datetime('now')),
          PRIMARY KEY (session_id, message_ref, kind, value)
        )
        """
    )
    # Autosuggest reads known values per resident + kind; the tag-cloud reads a
    # session's mentions. Index both access paths.
    op.execute(
        "CREATE INDEX IF NOT EXISTS mentions_owner_kind_idx "
        "ON mentions (owner_uid, kind, value)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS mentions_session_idx "
        "ON mentions (session_id, owner_uid)"
    )


def downgrade() -> None:
    # One-way, matching the other migrations: a downgrade would drop every
    # recorded mention.
    raise NotImplementedError(
        "Downgrade is not supported. Delete solaris.db and re-run upgrade if needed."
    )

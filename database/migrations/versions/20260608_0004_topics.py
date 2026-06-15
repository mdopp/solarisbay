"""add topics registry table

Revision ID: 0004_topics
Revises: 0003_voice_pe_rooms
Create Date: 2026-06-08

The topics registry (solaris-architecture.md §3): a cross-cutting, persistent
label grouping a theme/project/context across chats, notes, and future graph
nodes. `parent` gives hierarchy (`projekt/wintergarten` → `projekt`); `scope`
is per-resident by default (D3) and widens to `shared`/`admin`; a topic carries
a default model + persona (D2). The two built-in system topics (`household`,
`servicebay-admin`) are seeded as rows with null owner_uid.
"""

from __future__ import annotations

from alembic import op


revision = "0004_topics"
down_revision = "0003_voice_pe_rooms"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS topics (
          slug            TEXT PRIMARY KEY,
          display_name    TEXT NOT NULL,
          parent          TEXT REFERENCES topics(slug),
          scope           TEXT NOT NULL DEFAULT 'resident',
          owner_uid       TEXT,
          default_model   TEXT,
          default_persona TEXT,
          color           TEXT,
          archived        INTEGER NOT NULL DEFAULT 0,
          created_at      TEXT NOT NULL DEFAULT (datetime('now')),
          updated_at      TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS topics_owner_scope_idx ON topics (owner_uid, scope)"
    )
    op.execute("CREATE INDEX IF NOT EXISTS topics_parent_idx ON topics (parent)")

    # Built-in system topics (null owner_uid). household → e2b/shared,
    # servicebay-admin → 12b/admin.
    op.execute(
        """
        INSERT INTO topics (slug, display_name, scope, default_model) VALUES
          ('household',        'Household',        'shared', 'gemma4:e2b'),
          ('servicebay-admin', 'ServiceBay Admin', 'admin',  'gemma4:12b')
        ON CONFLICT(slug) DO NOTHING
        """
    )


def downgrade() -> None:
    # One-way, matching the other migrations: a downgrade would drop the
    # topic registry and any chat/data assignments referencing it.
    raise NotImplementedError(
        "Downgrade is not supported. Delete solaris.db and re-run upgrade if needed."
    )

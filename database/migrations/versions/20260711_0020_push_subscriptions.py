"""add push_subscriptions table (Web Push / VAPID, mobile epic #718 Phase 1a)

Revision ID: 0020_push_subscriptions
Revises: 0019_favorites
Create Date: 2026-07-11

The one operational table behind Web Push (#713): a resident's registered
PushSubscription endpoints, so a fired timer/reminder can fan a phone
notification out to every device the resident installed the PWA on. Per-resident
rows keyed by `owner_uid` (same operational-state precedent as
`favorites`/`topics`); the browser endpoint URL is the natural unique key, so a
re-subscribe upserts rather than duplicates. `push_store` degrades to empty when
this table is missing, so the engine is safe to deploy before the migration
lands.
"""

from __future__ import annotations

from alembic import op


revision = "0020_push_subscriptions"
down_revision = "0019_favorites"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS push_subscriptions (
          id         TEXT PRIMARY KEY,
          owner_uid  TEXT NOT NULL,
          endpoint   TEXT NOT NULL UNIQUE,
          p256dh     TEXT NOT NULL,
          auth       TEXT NOT NULL,
          user_agent TEXT NOT NULL DEFAULT '',
          created    TEXT NOT NULL DEFAULT (datetime('now')),
          last_ok    TEXT
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS push_subscriptions_owner_idx "
        "ON push_subscriptions (owner_uid)"
    )


def downgrade() -> None:
    raise NotImplementedError(
        "Downgrade is not supported. Delete solaris.db and re-run upgrade if needed."
    )

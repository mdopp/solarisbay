"""add device_tokens table (native-client bearer auth, mobile epic #718 Phase 3 #717)

Revision ID: 0021_device_tokens
Revises: 0020_push_subscriptions
Create Date: 2026-07-11

A native Android widget/tile can't ride the browser's Authelia cookies, so it
authenticates with a long-lived, per-resident bearer minted from an already
authenticated interactive session (#717). Each row is one minted token, scoped to
the owning resident (`owner_uid`). The plaintext token is NEVER stored — only its
`hashlib.sha256` hex digest, which is the unique lookup key; the plaintext is
returned to the caller exactly once at creation time. `device_token_store`
degrades to empty when this table is missing, so the engine is safe to deploy
before the migration lands.
"""

from __future__ import annotations

from alembic import op


revision = "0021_device_tokens"
down_revision = "0020_push_subscriptions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS device_tokens (
          id         TEXT PRIMARY KEY,
          owner_uid  TEXT NOT NULL,
          token_hash TEXT NOT NULL UNIQUE,
          label      TEXT,
          created    TEXT NOT NULL DEFAULT (datetime('now')),
          last_used  TEXT,
          revoked    INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS device_tokens_hash_idx "
        "ON device_tokens (token_hash)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS device_tokens_owner_idx "
        "ON device_tokens (owner_uid)"
    )


def downgrade() -> None:
    raise NotImplementedError(
        "Downgrade is not supported. Delete solaris.db and re-run upgrade if needed."
    )

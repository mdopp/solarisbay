"""add favorites + favorite_usage tables (start-page pins)

Revision ID: 0019_favorites
Revises: 0018_okf_vectors
Create Date: 2026-07-06

The two operational tables behind `pin_favorite` (#645): `favorites` holds a
resident's (or the shared `household`) pinned actions/entities/links for the
start page, `favorite_usage` counts how often a pinnable tool actually ran so
"häufig genutzt" is data-driven. Per-resident rows keyed by `owner_uid` — the
same operational-state precedent as `topics`/`mentions`, NOT the OKF vault and
NOT `gbrain`. `favorites_store` degrades to empty when these are missing, so the
engine is safe to deploy before this migration lands.
"""

from __future__ import annotations

from alembic import op


revision = "0019_favorites"
down_revision = "0018_okf_vectors"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS favorites (
          id        TEXT PRIMARY KEY,
          owner_uid TEXT NOT NULL,
          kind      TEXT NOT NULL CHECK (kind IN ('action','entity','link')),
          label     TEXT NOT NULL,
          payload   TEXT NOT NULL,
          position  INTEGER NOT NULL DEFAULT 0,
          created   TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS favorites_owner_position_idx "
        "ON favorites (owner_uid, position)"
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS favorite_usage (
          owner_uid    TEXT NOT NULL,
          kind         TEXT NOT NULL,
          payload_hash TEXT NOT NULL,
          payload      TEXT NOT NULL,
          count        INTEGER NOT NULL DEFAULT 0,
          last_used    TEXT NOT NULL DEFAULT (datetime('now')),
          PRIMARY KEY (owner_uid, payload_hash)
        )
        """
    )


def downgrade() -> None:
    raise NotImplementedError(
        "Downgrade is not supported. Delete solaris.db and re-run upgrade if needed."
    )

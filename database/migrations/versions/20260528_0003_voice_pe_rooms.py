"""add voice_pe_rooms table

Revision ID: 0003_voice_pe_rooms
Revises: 0002_add_token_counts
Create Date: 2026-05-28

Maps a voice satellite (keyed by its gatekeeper client id — the socket
peer host) to a room, so the gatekeeper can tell Hermes which room a
command came from. Rows are self-enrolled by conversation (the gatekeeper
POST /room endpoint; see issue #94). Interim store — longer term the
device->area mapping should be sourced from Home Assistant as the single
source of truth.
"""

from __future__ import annotations

from alembic import op


revision = "0003_voice_pe_rooms"
down_revision = "0002_add_token_counts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE voice_pe_rooms (
            satellite_id TEXT PRIMARY KEY,
            room         TEXT NOT NULL,
            updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE voice_pe_rooms")

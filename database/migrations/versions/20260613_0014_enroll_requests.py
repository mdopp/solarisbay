"""add enroll_requests table

Revision ID: 0014_enroll_requests
Revises: 0013_pending_residents
Create Date: 2026-06-13

The reverse enroll-stash channel for live-voice onboarding (#376). The engine's
registration flow writes a row here for the candidate uid; the gatekeeper — when
it is HA's Wyoming STT provider — reads the pending row, captures the current
speaker's PCM across the "say your name" turns, and after N samples enrols the
voice in-process and writes the result back (status `done`/`failed`). The engine
then reads the row to confirm enrolment.

Only status/count/result live here — the raw biometric PCM is accumulated in the
gatekeeper process and never persisted; the durable artifact is the averaged
embedding in `voice_embeddings`. `created_at` bounds a TTL so a request that no
gatekeeper picks up (speaker-ID off) times out honestly instead of hanging.
"""

from __future__ import annotations

from alembic import op


revision = "0014_enroll_requests"
down_revision = "0013_pending_residents"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE enroll_requests (
            uid            TEXT PRIMARY KEY,
            status         TEXT NOT NULL DEFAULT 'pending',
            target_samples INTEGER NOT NULL DEFAULT 3,
            collected      INTEGER NOT NULL DEFAULT 0,
            result         TEXT,
            created_at     TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )


def downgrade() -> None:
    raise NotImplementedError(
        "Downgrade is not supported. Delete solilos.db and re-run upgrade if needed."
    )

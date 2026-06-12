"""add voice_profiles table for resident speaker embeddings

Revision ID: 0012_voice_profiles
Revises: 0011_session_traces_tool_steps
Create Date: 2026-06-12

Per-resident voice profile for speaker identification (#349). One row per
resident: the speaker `embedding` (a packed float vector, stored as BLOB) and
the enrollment metadata the lifecycle needs (`enroll_count` = how many
utterances contributed, `enrolled_at`, `updated_at`). Consumed later by the
speaker matcher (#350) and onboarding enrollment (#354).

This is biometric data, so it is kept local-only (solilos.db, never leaves the
box), scoped per resident by `owner_uid`, and hard-deletable on request — the
same privacy posture as the per-resident trace scoping (D3). `owner_uid` is the
primary key: a resident has exactly one current voice profile, re-enrollment
replaces it.

The baseline (0001) already provisions a Hermes-era `voice_embeddings` table
(gatekeeper-side store). This table is the Sol Engine home for the lifecycle the
post-Hermes consumers (#350/#354) use; unifying the two is a draft-review call.
"""

from __future__ import annotations

from alembic import op


revision = "0012_voice_profiles"
down_revision = "0011_session_traces_tool_steps"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS voice_profiles (
          owner_uid     TEXT NOT NULL PRIMARY KEY,
          embedding     BLOB NOT NULL,
          enroll_count  INTEGER NOT NULL,
          enrolled_at   TEXT NOT NULL DEFAULT (datetime('now')),
          updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )


def downgrade() -> None:
    # One-way, matching the other migrations: a downgrade would drop every
    # enrolled voice profile.
    raise NotImplementedError(
        "Downgrade is not supported. Delete solilos.db and re-run upgrade if needed."
    )

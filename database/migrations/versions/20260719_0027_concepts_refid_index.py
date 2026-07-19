"""add the concepts(ref_id, ref_kind) index the write path's dedup lookup needs

Revision ID: 0027_concepts_refid_index
Revises: 0026_entity_resolution_index
Create Date: 2026-07-19

Perf fix, part 2 (box-verify). With 0026 indexing `resolve_entity`, the ingest's
next per-item bottleneck surfaced: `OkfWriter._existing_concept` runs
`SELECT id, content_hash FROM concepts WHERE ref_id = ? AND ref_kind = ?` once
per ingested item, but `concepts` had only its primary-key autoindex — so that
lookup full-scanned the concepts table (~87k rows: entities + events). This
composite index turns it into an index seek, removing the last O(n^2) scan from
the per-boot idempotency pass.
"""

from __future__ import annotations

from alembic import op


revision = "0027_concepts_refid_index"
down_revision = "0026_entity_resolution_index"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX IF NOT EXISTS concepts_ref_idx "
        "ON concepts (ref_id, ref_kind)"
    )


def downgrade() -> None:
    raise NotImplementedError(
        "Downgrade is not supported. Delete solaris.db and re-run upgrade if needed."
    )

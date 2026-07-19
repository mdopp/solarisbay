"""add the entity-resolution index that resolve_entity's dedup lookup needs

Revision ID: 0026_entity_resolution_index
Revises: 0025_engine_import_jobs
Create Date: 2026-07-19

Perf fix (box-verify of the #873 substrate). `projection.resolve_entity` runs
`SELECT id FROM entities WHERE resident_uid = ? AND type = ? AND canonical_name = ?`
once per ingested item to dedup it. `entities` had only its primary-key
autoindex (on `id`), so that lookup was a full table scan. On the real library
(~12k entities, ~46k Jellyfin items re-checked every boot for idempotency) that
made the ingest O(n^2) — a ~15-20 min, ever-growing per-boot CPU burn that also
delayed the legacy-song prune behind it. This composite index turns the lookup
into an index seek, so the ingest is O(n log n). `entity_aliases` already has
`entity_aliases_alias_idx` for the alias fallback path.
"""

from __future__ import annotations

from alembic import op


revision = "0026_entity_resolution_index"
down_revision = "0025_engine_import_jobs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX IF NOT EXISTS entities_resolve_idx "
        "ON entities (resident_uid, type, canonical_name)"
    )


def downgrade() -> None:
    raise NotImplementedError(
        "Downgrade is not supported. Delete solaris.db and re-run upgrade if needed."
    )

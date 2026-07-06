"""add okf_vectors store (rebuildable embedding vectors)

Revision ID: 0018_okf_vectors
Revises: 0017_session_traces_detail_body
Create Date: 2026-07-06

The vector store the OKF embedding-queue drain fills (#650, docs/okf-write-contract.md
§1). One row per `concepts.embedding_id`, holding the `nomic-embed-text` vector as a
float32 little-endian BLOB (np.tobytes()). Derived + rebuildable: drop the table and the
next drain refills it from the JSONL queue / a re-ingest. `concept_id` carries the
writer's `ref_id` (the entity/event id), NOT `concepts.id` — retrieval joins through
`concepts.embedding_id = okf_vectors.embedding_id`.
"""

from __future__ import annotations

from alembic import op


revision = "0018_okf_vectors"
down_revision = "0017_session_traces_detail_body"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS okf_vectors (
          embedding_id TEXT PRIMARY KEY,
          concept_id   TEXT NOT NULL,
          model        TEXT NOT NULL,
          dim          INTEGER NOT NULL,
          vector       BLOB NOT NULL,
          updated      TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )


def downgrade() -> None:
    raise NotImplementedError(
        "Downgrade is not supported. Delete solaris.db and re-run upgrade if needed."
    )

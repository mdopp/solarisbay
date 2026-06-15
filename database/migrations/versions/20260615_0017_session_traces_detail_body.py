"""add detail_json to session_traces

Revision ID: 0017_session_traces_detail_body
Revises: 0016_okf_knowledge_index
Create Date: 2026-06-15

Persist each LLM step's exact request/response body *with* the turn trace
(#451). Until now the step list was persisted but `detail_id` pointed at the
in-process trace recorder's ephemeral detail ring (ids restart at 0 per
process), so after a page reload / engine restart the detail modal 404'd. The
body now lives in `detail_json` next to the step, so a reopened turn renders its
exact content too. Nullable so pre-existing rows + tool steps (no body) stay
valid.
"""

from __future__ import annotations

from alembic import op


revision = "0017_session_traces_detail_body"
down_revision = "0016_okf_knowledge_index"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE session_traces ADD COLUMN detail_json TEXT")


def downgrade() -> None:
    # One-way, matching the other migrations: a downgrade would drop the column.
    raise NotImplementedError(
        "Downgrade is not supported. Delete solaris.db and re-run upgrade if needed."
    )

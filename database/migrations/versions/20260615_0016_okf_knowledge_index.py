"""add OKF knowledge-index tables (rebuildable projection)

Revision ID: 0016_okf_knowledge_index
Revises: 0015_pending_resident_request
Create Date: 2026-06-15

The knowledge-index projection from the OKF write contract (#204/#446,
docs/okf-write-contract.md §4). OKF concept files in `notes/okf/` are the source
of truth for household knowledge; these tables are a *rebuildable projection* of
them (and embeddings are derived) — `git clone` the vault and the whole index
rebuilds from scratch. This is NOT the operational `.db` schema (sessions,
timers, speaker-ID) and NOT `gbrain` (the read/retrieval side).

Every knowledge row carries `resident_uid` (an owning resident uid, or
`household` for shared). Ingestion adapters (Immich/calendar/contacts/Obsidian)
write here via the shared OKF writer (#447): resolve/create `entities` (alias
dedup via `entity_aliases` + `ingest_log`), project `facts`/`events`/
`event_entities`, and link the OKF file + embedding through `concepts`.
`ingest_log` keeps re-ingestion idempotent — an unchanged `content_hash` for a
`(source, external_id)` pair is a skip.
"""

from __future__ import annotations

from alembic import op


revision = "0016_okf_knowledge_index"
down_revision = "0015_pending_resident_request"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS entities (
          id             TEXT PRIMARY KEY,
          type           TEXT NOT NULL,
          canonical_name TEXT NOT NULL,
          resident_uid   TEXT NOT NULL,
          source         TEXT NOT NULL,
          content_hash   TEXT NOT NULL,
          updated        TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS entity_aliases (
          entity_id TEXT NOT NULL,
          alias     TEXT NOT NULL,
          PRIMARY KEY (entity_id, alias),
          FOREIGN KEY (entity_id) REFERENCES entities (id)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS entity_aliases_alias_idx ON entity_aliases (alias)"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS facts (
          id                TEXT PRIMARY KEY,
          subject_entity_id TEXT,
          resident_uid      TEXT NOT NULL,
          predicate         TEXT NOT NULL,
          value             TEXT NOT NULL,
          confidence        REAL,
          source            TEXT NOT NULL,
          timestamp         TEXT NOT NULL DEFAULT (datetime('now')),
          FOREIGN KEY (subject_entity_id) REFERENCES entities (id)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS facts_subject_predicate_idx "
        "ON facts (subject_entity_id, predicate)"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS events (
          id           TEXT PRIMARY KEY,
          ts           TEXT NOT NULL,
          resident_uid TEXT NOT NULL,
          kind         TEXT NOT NULL,
          source       TEXT NOT NULL
        )
        """
    )
    op.execute("CREATE INDEX IF NOT EXISTS events_ts_idx ON events (ts)")
    op.execute(
        "CREATE INDEX IF NOT EXISTS events_resident_ts_idx ON events (resident_uid, ts)"
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS event_entities (
          event_id  TEXT NOT NULL,
          entity_id TEXT NOT NULL,
          role      TEXT NOT NULL,
          PRIMARY KEY (event_id, entity_id, role),
          FOREIGN KEY (event_id) REFERENCES events (id),
          FOREIGN KEY (entity_id) REFERENCES entities (id)
        )
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS concepts (
          id           TEXT PRIMARY KEY,
          ref_id       TEXT NOT NULL,
          ref_kind     TEXT NOT NULL CHECK (ref_kind IN ('entity', 'event')),
          okf_path     TEXT NOT NULL,
          embedding_id TEXT,
          content_hash TEXT NOT NULL,
          updated      TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )

    op.execute(
        """
        CREATE TABLE IF NOT EXISTS ingest_log (
          source       TEXT NOT NULL,
          external_id  TEXT NOT NULL,
          content_hash TEXT NOT NULL,
          ingested_at  TEXT NOT NULL DEFAULT (datetime('now')),
          PRIMARY KEY (source, external_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ingest_log_source_external_idx "
        "ON ingest_log (source, external_id)"
    )


def downgrade() -> None:
    raise NotImplementedError(
        "Downgrade is not supported. Delete solaris.db and re-run upgrade if needed."
    )

"""The rebuildable `.db` projection of OKF concepts (#446, contract §4).

Synchronous sqlite3 on a local file — the same point read/write pattern as
`engine/store.py`. These tables are derived from the OKF files (the source of
truth), so every write here is an upsert keyed by the stable id the OKF file
carries in its frontmatter.
"""

from __future__ import annotations

import sqlite3
import uuid
from typing import Any


def _conn(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 10000")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# --- entity resolution / dedup ------------------------------------------------


def resolve_entity(
    conn: sqlite3.Connection,
    *,
    type: str,
    canonical_name: str,
    resident_uid: str,
    aliases: list[str],
) -> str | None:
    """Find an existing entity id for this resident by canonical name or alias.

    Dedup is per-resident (§6): a `mdopp`-scoped "Anna" never collides with a
    `lena`-scoped one. Returns the entity id or ``None`` if new.
    """
    row = conn.execute(
        "SELECT id FROM entities"
        " WHERE resident_uid = ? AND type = ? AND canonical_name = ?",
        (resident_uid, type, canonical_name),
    ).fetchone()
    if row is not None:
        return row["id"]
    for alias in [canonical_name, *aliases]:
        row = conn.execute(
            "SELECT e.id FROM entities e"
            " JOIN entity_aliases a ON a.entity_id = e.id"
            " WHERE e.resident_uid = ? AND e.type = ? AND a.alias = ?",
            (resident_uid, type, alias),
        ).fetchone()
        if row is not None:
            return row["id"]
    return None


def upsert_entity(
    conn: sqlite3.Connection,
    *,
    entity_id: str,
    is_new: bool,
    type: str,
    canonical_name: str,
    resident_uid: str,
    source: str,
    content_hash: str,
    aliases: list[str],
) -> str:
    """Insert a new entity (`is_new`) or update the existing one with the given
    id; (re)records its aliases. The id is chosen by the writer (it is also the
    OKF file's frontmatter `id`), not generated here. Returns the entity id."""
    if is_new:
        conn.execute(
            "INSERT INTO entities"
            " (id, type, canonical_name, resident_uid, source, content_hash)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (entity_id, type, canonical_name, resident_uid, source, content_hash),
        )
    else:
        conn.execute(
            "UPDATE entities SET canonical_name = ?, source = ?,"
            " content_hash = ?, updated = datetime('now') WHERE id = ?",
            (canonical_name, source, content_hash, entity_id),
        )
    for alias in dict.fromkeys([canonical_name, *aliases]):
        conn.execute(
            "INSERT OR IGNORE INTO entity_aliases (entity_id, alias) VALUES (?, ?)",
            (entity_id, alias),
        )
    return entity_id


def entity_id_for_okf_path(conn: sqlite3.Connection, okf_path: str) -> str | None:
    """The entity id behind an OKF link path (e.g. ``people/anna`` →
    ``okf/people/anna.md``). Event participants are written as ``[[people/...]]``
    links; this maps a link target back to its `.db` entity for `event_entities`.
    """
    candidate = okf_path if okf_path.endswith(".md") else f"{okf_path}.md"
    for path in (candidate, f"okf/{candidate}"):
        row = conn.execute(
            "SELECT ref_id FROM concepts WHERE ref_kind = 'entity' AND okf_path = ?",
            (path,),
        ).fetchone()
        if row is not None:
            return row["ref_id"]
    return None


def upsert_event(
    conn: sqlite3.Connection,
    *,
    event_id: str,
    is_new: bool,
    ts: str,
    resident_uid: str,
    kind: str,
    source: str,
) -> str:
    if is_new:
        conn.execute(
            "INSERT INTO events (id, ts, resident_uid, kind, source)"
            " VALUES (?, ?, ?, ?, ?)",
            (event_id, ts, resident_uid, kind, source),
        )
    else:
        conn.execute(
            "UPDATE events SET ts = ?, kind = ?, source = ? WHERE id = ?",
            (ts, kind, source, event_id),
        )
    return event_id


def set_event_entities(
    conn: sqlite3.Connection,
    *,
    event_id: str,
    members: list[tuple[str, str]],
) -> None:
    """Replace an event's (entity_id, role) edges — the projection mirrors the
    OKF Relationships section, so a re-ingest replaces rather than accretes."""
    conn.execute("DELETE FROM event_entities WHERE event_id = ?", (event_id,))
    for entity_id, role in members:
        conn.execute(
            "INSERT OR IGNORE INTO event_entities (event_id, entity_id, role)"
            " VALUES (?, ?, ?)",
            (event_id, entity_id, role),
        )


def replace_facts(
    conn: sqlite3.Connection,
    *,
    subject_entity_id: str,
    resident_uid: str,
    source: str,
    facts: list[tuple[str, str, float | None]],
) -> None:
    """Replace this subject's projected facts (predicate, value, confidence).

    Facts are authored in OKF and projected here (§4/§5); a re-ingest replaces
    the subject's rows so the projection can't drift from the file.
    """
    conn.execute("DELETE FROM facts WHERE subject_entity_id = ?", (subject_entity_id,))
    for predicate, value, confidence in facts:
        conn.execute(
            "INSERT INTO facts"
            " (id, subject_entity_id, resident_uid, predicate, value, confidence, source)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                uuid.uuid4().hex,
                subject_entity_id,
                resident_uid,
                predicate,
                value,
                confidence,
                source,
            ),
        )


def upsert_concept(
    conn: sqlite3.Connection,
    *,
    ref_id: str,
    ref_kind: str,
    okf_path: str,
    content_hash: str,
    embedding_id: str | None,
) -> str:
    """Upsert the cross-layer `concepts` link row keyed by `(ref_id, ref_kind)`.

    Returns the concept id. A re-ingest of the same ref updates the row in
    place (and its `content_hash`/`embedding_id`) rather than creating a dup.
    """
    row = conn.execute(
        "SELECT id FROM concepts WHERE ref_id = ? AND ref_kind = ?",
        (ref_id, ref_kind),
    ).fetchone()
    if row is None:
        concept_id = uuid.uuid4().hex
        conn.execute(
            "INSERT INTO concepts"
            " (id, ref_id, ref_kind, okf_path, embedding_id, content_hash)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (concept_id, ref_id, ref_kind, okf_path, embedding_id, content_hash),
        )
        return concept_id
    concept_id = row["id"]
    conn.execute(
        "UPDATE concepts SET okf_path = ?, embedding_id = ?, content_hash = ?,"
        " updated = datetime('now') WHERE id = ?",
        (okf_path, embedding_id, content_hash, concept_id),
    )
    return concept_id


def concept_embedding_id(conn: sqlite3.Connection, concept_id: str) -> str | None:
    row = conn.execute(
        "SELECT embedding_id FROM concepts WHERE id = ?", (concept_id,)
    ).fetchone()
    return row["embedding_id"] if row else None


# --- ingest_log (idempotency) -------------------------------------------------


def ingest_log_hash(
    conn: sqlite3.Connection, source: str, external_id: str
) -> str | None:
    row = conn.execute(
        "SELECT content_hash FROM ingest_log WHERE source = ? AND external_id = ?",
        (source, external_id),
    ).fetchone()
    return row["content_hash"] if row else None


def record_ingest(
    conn: sqlite3.Connection,
    *,
    source: str,
    external_id: str,
    content_hash: str,
) -> None:
    conn.execute(
        "INSERT INTO ingest_log (source, external_id, content_hash, ingested_at)"
        " VALUES (?, ?, ?, datetime('now'))"
        " ON CONFLICT (source, external_id)"
        " DO UPDATE SET content_hash = excluded.content_hash,"
        " ingested_at = excluded.ingested_at",
        (source, external_id, content_hash),
    )


def open_conn(db_path: str) -> sqlite3.Connection:
    return _conn(db_path)


def row_count(conn: sqlite3.Connection, table: str) -> int:
    return conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]  # noqa: S608


def fetch_all(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[Any]:
    return conn.execute(sql, params).fetchall()

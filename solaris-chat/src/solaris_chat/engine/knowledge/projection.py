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
    # 10s (matches the chat store) so the long boot ingest — now heavier per item
    # after the incremental FTS write (#830) — waits out a WAL checkpoint or a
    # contending poller instead of raising "database is locked" and dropping the
    # row (#835); busy_timeout makes a blocked writer wait instead of raising.
    conn.execute("PRAGMA busy_timeout = 10000")
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL (persisted in the db header) so the ingest writer and concurrent
    # chat-turn readers/writers don't immediately hit "database is locked" (#600).
    conn.execute("PRAGMA journal_mode = WAL")
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

    A link path is `okf/`-relative and carries no owner prefix, but a
    private concept is stored under ``users/<uid>/okf/...`` (#576). So also match
    any row whose path ENDS WITH ``/okf/<candidate>`` (the per-user subtree)."""
    candidate = okf_path if okf_path.endswith(".md") else f"{okf_path}.md"
    for path in (candidate, f"okf/{candidate}"):
        row = conn.execute(
            "SELECT ref_id FROM concepts WHERE ref_kind = 'entity' AND okf_path = ?",
            (path,),
        ).fetchone()
        if row is not None:
            return row["ref_id"]
    row = conn.execute(
        "SELECT ref_id FROM concepts WHERE ref_kind = 'entity' AND okf_path LIKE ?",
        (f"users/%/okf/{candidate}",),
    ).fetchone()
    return row["ref_id"] if row is not None else None


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


def concepts_changed_since(conn: sqlite3.Connection, watermark: str) -> list[str]:
    """OKF paths of concepts whose `updated` is newer than `watermark` (#653).

    Bounded-input source for the nightly Bibliothekar: `concepts.updated` is a
    UTC `datetime('now')` string, so `watermark` must be the same naive-UTC form.
    An empty watermark yields every concept path (a first run)."""
    rows = conn.execute(
        "SELECT okf_path FROM concepts WHERE updated > ? ORDER BY updated DESC",
        (watermark or "",),
    ).fetchall()
    return [r["okf_path"] for r in rows]


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


# --- per-source sync cursor (#529) --------------------------------------------
#
# Reuses the ingest_log table (a free-text content_hash column) with a reserved
# external_id so a source's incremental cursor (Immich high-water `when`, a DAV
# sync token) persists across boots without a new table/migration.

_CURSOR_KEY = "__cursor__"


def get_cursor(conn: sqlite3.Connection, source: str) -> str:
    """The persisted incremental cursor for a source, "" when none yet."""
    row = conn.execute(
        "SELECT content_hash FROM ingest_log WHERE source = ? AND external_id = ?",
        (source, _CURSOR_KEY),
    ).fetchone()
    return row["content_hash"] if row else ""


def set_cursor(conn: sqlite3.Connection, source: str, cursor: str) -> None:
    record_ingest(conn, source=source, external_id=_CURSOR_KEY, content_hash=cursor)


# --- concept page aggregation (#502 phase 1) ----------------------------------


def resolve_entity_id(
    conn: sqlite3.Connection, ref: str, resident_uid: str
) -> str | None:
    """Resolve a page ref to an entity id for this resident.

    `ref` may already be the entity id, a canonical name, or a recorded alias.
    Per-resident (§6) so one resident's "Anna" never resolves to another's.
    Returns the entity id or ``None`` when nothing matches.
    """
    row = conn.execute(
        "SELECT id FROM entities WHERE id = ? AND resident_uid = ?",
        (ref, resident_uid),
    ).fetchone()
    if row is not None:
        return row["id"]
    row = conn.execute(
        "SELECT id FROM entities WHERE resident_uid = ? AND canonical_name = ?",
        (resident_uid, ref),
    ).fetchone()
    if row is not None:
        return row["id"]
    row = conn.execute(
        "SELECT e.id FROM entities e"
        " JOIN entity_aliases a ON a.entity_id = e.id"
        " WHERE e.resident_uid = ? AND a.alias = ?",
        (resident_uid, ref),
    ).fetchone()
    return row["id"] if row is not None else None


def entity_row(conn: sqlite3.Connection, entity_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT id, type, canonical_name, resident_uid, source FROM entities"
        " WHERE id = ?",
        (entity_id,),
    ).fetchone()
    return dict(row) if row is not None else None


def entity_aliases(conn: sqlite3.Connection, entity_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT alias FROM entity_aliases WHERE entity_id = ? ORDER BY alias",
        (entity_id,),
    ).fetchall()
    return [r["alias"] for r in rows]


def linkable_aliases(
    conn: sqlite3.Connection, resident_uid: str
) -> list[tuple[str, str]]:
    """(alias, entity_id) pairs for the client's auto-linkify index (#694).

    Canonical names plus recorded aliases for the resident's OKF entities, each
    pointing at the entity id its concept page resolves under. Per-resident (§6)
    so one resident's "Anna" never links to another's. The caller caps/filters
    the list; this just unions both name sources.
    """
    rows = conn.execute(
        "SELECT e.canonical_name AS alias, e.id AS id FROM entities e"
        " WHERE e.resident_uid = ?"
        " UNION"
        " SELECT a.alias AS alias, e.id AS id FROM entity_aliases a"
        " JOIN entities e ON e.id = a.entity_id"
        " WHERE e.resident_uid = ?",
        (resident_uid, resident_uid),
    ).fetchall()
    return [(r["alias"], r["id"]) for r in rows]


def entity_okf_path(conn: sqlite3.Connection, entity_id: str) -> str | None:
    row = conn.execute(
        "SELECT okf_path FROM concepts WHERE ref_kind = 'entity' AND ref_id = ?",
        (entity_id,),
    ).fetchone()
    return row["okf_path"] if row is not None else None


# The shared-data sentinel resident_uid (mirrors EngineProfile.default_uid).
# A row owned by this uid is visible to every resident.
SHARED_UID = "household"


def entity_facts(
    conn: sqlite3.Connection, entity_id: str, caller_uid: str
) -> list[dict[str, Any]]:
    """This entity's projected facts the caller may see (#576).

    Per-owner scope (required `caller_uid`, no unscoped default — every read
    threads an identity): only `resident_uid IN (caller, 'household')`. An
    unknown/voice caller is `household`, so it sees only shared facts — never
    another resident's personal fact."""
    rows = conn.execute(
        "SELECT predicate, value, confidence FROM facts"
        " WHERE subject_entity_id = ? AND resident_uid IN (?, ?)"
        " ORDER BY predicate, value",
        (entity_id, caller_uid, SHARED_UID),
    ).fetchall()
    return [dict(r) for r in rows]


def entity_events(
    conn: sqlite3.Connection, entity_id: str, caller_uid: str
) -> list[dict[str, Any]]:
    """Events this entity participates in (newest first), with its role.

    Per-owner scope (required `caller_uid`, #576): only `resident_uid IN
    (caller, 'household')`, so an event recorded under another resident never
    surfaces for the caller."""
    rows = conn.execute(
        "SELECT ev.id, ev.ts, ev.kind, ee.role FROM event_entities ee"
        " JOIN events ev ON ev.id = ee.event_id"
        " WHERE ee.entity_id = ? AND ev.resident_uid IN (?, ?)"
        " ORDER BY ev.ts DESC",
        (entity_id, caller_uid, SHARED_UID),
    ).fetchall()
    return [dict(r) for r in rows]


def events_between(
    conn: sqlite3.Connection,
    caller_uid: str,
    after: str | None,
    before: str | None,
) -> list[dict[str, Any]]:
    """Events in the `[after, before]` ISO range the caller may see (#651).

    Per-owner scope (#576): only `resident_uid IN (caller, 'household')`. `after`/
    `before` compare as ISO strings against `events.ts` (the `events(resident_uid,
    ts)` index from migration 0016). Returns newest first, each with its `okf_path`
    (from `concepts`, `ref_kind='event'`) and the participant names joined from
    `event_entities → entities`, so the caller can answer "wen…" without a read."""
    where = ["ev.resident_uid IN (?, ?)"]
    params: list[str] = [caller_uid, SHARED_UID]
    if after:
        where.append("ev.ts >= ?")
        params.append(after)
    if before:
        where.append("ev.ts <= ?")
        params.append(before)
    rows = conn.execute(
        "SELECT ev.id, ev.ts, ev.kind, c.okf_path,"
        " group_concat(en.canonical_name, ', ') AS participants"
        " FROM events ev"
        " LEFT JOIN concepts c ON c.ref_kind = 'event' AND c.ref_id = ev.id"
        " LEFT JOIN event_entities ee ON ee.event_id = ev.id"
        " LEFT JOIN entities en ON en.id = ee.entity_id"
        f" WHERE {' AND '.join(where)}"  # noqa: S608 — where clauses are literals
        " GROUP BY ev.id ORDER BY ev.ts DESC",
        tuple(params),
    ).fetchall()
    return [dict(r) for r in rows]


# --- P1c prune (ADR 0002/B7): drop legacy per-song OKF artifacts ---------------


def legacy_projection_only_concepts(
    conn: sqlite3.Connection, *, source: str, type: str
) -> list[dict[str, Any]]:
    """The pre-P1b per-item artifacts for a now-projection-only (source, type).

    A projection-only concept (#877) carries NO `concepts` link row, so a `song`
    entity that still has one is a stale pre-switch artifact. Returns one row per
    such concept — its entity id, `concepts.id`, `okf_path`, and `embedding_id` —
    for the caller to delete file + FTS + rows. After the caller's delete pass the
    join finds nothing, so a second run is a no-op (idempotent)."""
    rows = conn.execute(
        "SELECT c.id AS concept_id, c.ref_id AS entity_id, c.okf_path AS okf_path,"
        " c.embedding_id AS embedding_id"
        " FROM concepts c JOIN entities e ON e.id = c.ref_id"
        " WHERE c.ref_kind = 'entity' AND e.type = ? AND e.source = ?",
        (type, source),
    ).fetchall()
    return [dict(r) for r in rows]


def delete_concept_artifacts(
    conn: sqlite3.Connection, *, concept_id: str, embedding_id: str | None
) -> None:
    """Delete the `concepts` link row + its `okf_vectors` embedding, keeping the
    entity + its facts (so a pruned concept matches a fresh projection-only one).
    The caller unlinks the markdown file + FTS row and commits."""
    if embedding_id:
        conn.execute("DELETE FROM okf_vectors WHERE embedding_id = ?", (embedding_id,))
    conn.execute("DELETE FROM concepts WHERE id = ?", (concept_id,))


def open_conn(db_path: str) -> sqlite3.Connection:
    return _conn(db_path)


def row_count(conn: sqlite3.Connection, table: str) -> int:
    return conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]  # noqa: S608


def fetch_all(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[Any]:
    return conn.execute(sql, params).fetchall()

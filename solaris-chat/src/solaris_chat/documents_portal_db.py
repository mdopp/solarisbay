"""DB-backed document category views (#doc).

An uploaded document is extracted into a `document` entity carrying typed facts
(`category`, `provider`, `policy_number`, `cancellation_deadline`, …) — see
`engine/ingest/obsidian.py::_ingest_document`. The markdown renderer can't do
tables, so the Notizen "documents" page renders these DB-backed instead: one
doorway per category, and a table per category built straight from the
`entities`+`facts` projection (mirrors `notes_portal_db.py`).

Owner scope is the entity's `resident_uid`: the caller sees their own documents
plus the shared household pool, never another resident's (matches
`projection.entity_facts` scoping). Each fact carries its `confidence`, so the
UI can flag an unconfirmed (agent-extracted, 0.6) value vs. a human-confirmed
(1.0) one; per predicate the highest-confidence value wins.

Returns ``None`` when the projection is absent (fresh install / migration not
run) so the caller can degrade gracefully.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from solaris_chat import notes_search

_REQUIRED_TABLES = ("entities", "facts")


def _connect(db_path: str) -> sqlite3.Connection | None:
    if not Path(db_path).exists():
        return None
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    try:
        have = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    except sqlite3.OperationalError:
        conn.close()
        return None
    if not set(_REQUIRED_TABLES).issubset(have):
        conn.close()
        return None
    return conn


def _scope(uid: str) -> tuple[str, list[str]]:
    """`resident_uid` owner scope: the caller's own documents + shared household."""
    return "e.resident_uid IN (?, ?)", [uid, notes_search.SHARED_UID]


def categories(db_path: str, uid: str) -> dict[str, int] | None:
    """`{category: count}` for the caller's document doorways (own ∪ shared)."""
    conn = _connect(db_path)
    if conn is None:
        return None
    scope, params = _scope(uid)
    try:
        rows = conn.execute(
            "SELECT f.value AS category, COUNT(DISTINCT e.id) AS n"
            " FROM entities e JOIN facts f ON f.subject_entity_id = e.id"
            f" WHERE e.type = 'document' AND f.predicate = 'category' AND {scope}"  # noqa: S608
            " GROUP BY f.value ORDER BY n DESC, category",
            params,
        ).fetchall()
    finally:
        conn.close()
    return {r["category"]: r["n"] for r in rows}


_CONTACT_PREDICATES = ("phone", "email", "address", "contact_person")


def contacts(db_path: str, uid: str) -> list[dict[str, Any]] | None:
    """The phone-book: every `organization` (a document's provider) in scope with
    its contact facts and the documents grouped under it (#doc-graph).

    Documents join to their org by name — the document's `provider` fact equals
    the org's `canonical_name` (see `_ingest_provider_org`). Contact facts follow
    the same highest-confidence-per-predicate rule as `category_view`, so a
    corrected phone number wins over the agent-extracted one."""
    conn = _connect(db_path)
    if conn is None:
        return None
    scope, params = _scope(uid)
    try:
        orgs = conn.execute(
            "SELECT DISTINCT e.id AS id, e.canonical_name AS name"
            f" FROM entities e WHERE e.type = 'organization' AND {scope}"  # noqa: S608
            " ORDER BY e.canonical_name",
            params,
        ).fetchall()
        out: list[dict[str, Any]] = []
        for org in orgs:
            contact: dict[str, dict[str, Any]] = {}
            for f in conn.execute(
                "SELECT predicate, value, confidence FROM facts"
                " WHERE subject_entity_id = ? ORDER BY confidence DESC",
                (org["id"],),
            ).fetchall():
                if (
                    f["predicate"] in _CONTACT_PREDICATES
                    and f["predicate"] not in contact
                ):
                    contact[f["predicate"]] = {
                        "value": f["value"],
                        "confidence": f["confidence"],
                    }
            # The org's documents: any `document` whose `provider` names it.
            docs = conn.execute(
                "SELECT DISTINCT e.id AS id, e.canonical_name AS title,"
                " (SELECT value FROM facts WHERE subject_entity_id = e.id"
                "  AND predicate = 'category' LIMIT 1) AS category"
                " FROM entities e JOIN facts f ON f.subject_entity_id = e.id"
                f" WHERE e.type = 'document' AND f.predicate = 'provider'"  # noqa: S608
                f" AND f.value = ? AND {scope}"
                " ORDER BY e.canonical_name",
                [org["name"], *params],
            ).fetchall()
            out.append(
                {
                    "entity_id": org["id"],
                    "name": org["name"],
                    "contact": contact,
                    "documents": [
                        {
                            "entity_id": d["id"],
                            "title": d["title"],
                            "category": d["category"],
                        }
                        for d in docs
                    ],
                }
            )
    finally:
        conn.close()
    return out


def category_view(db_path: str, uid: str, category: str) -> list[dict[str, Any]] | None:
    """The table rows for one category: per document, its title + fact map.

    Each fact is `{value, confidence}`; per predicate the highest-confidence
    value wins (a human-confirmed 1.0 beats the agent-extracted 0.6), so a
    corrected field displays as authoritative."""
    conn = _connect(db_path)
    if conn is None:
        return None
    scope, params = _scope(uid)
    try:
        docs = conn.execute(
            "SELECT DISTINCT e.id AS id, e.canonical_name AS title"
            " FROM entities e JOIN facts f ON f.subject_entity_id = e.id"
            " WHERE e.type = 'document' AND f.predicate = 'category'"
            f" AND f.value = ? AND {scope}"  # noqa: S608
            " ORDER BY e.canonical_name",
            [category, *params],
        ).fetchall()
        rows: list[dict[str, Any]] = []
        for doc in docs:
            facts: dict[str, dict[str, Any]] = {}
            # confidence DESC → NULL last in sqlite, so the first row per
            # predicate is the highest-confidence (confirmed) value.
            for f in conn.execute(
                "SELECT predicate, value, confidence FROM facts"
                " WHERE subject_entity_id = ? ORDER BY confidence DESC",
                (doc["id"],),
            ).fetchall():
                if f["predicate"] not in facts:
                    facts[f["predicate"]] = {
                        "value": f["value"],
                        "confidence": f["confidence"],
                    }
            rows.append(
                {
                    "entity_id": doc["id"],
                    "title": doc["title"],
                    "facts": facts,
                    "source_document": facts.get("source_document", {}).get("value"),
                }
            )
    finally:
        conn.close()
    return rows

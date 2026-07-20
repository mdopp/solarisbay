"""DB-backed document category views + the correction endpoint (#doc).

`documents_portal_db` tables the `document` entities + facts per category, owner-
scoped, with the highest-confidence value winning per predicate (a human-
confirmed 1.0 over the agent-extracted 0.6). The `/api/portal/documents/correct`
endpoint writes that confirmation.
"""

from __future__ import annotations

import sqlite3

from solaris_chat import documents_portal_db

_SCHEMA = """
CREATE TABLE entities (
  id TEXT PRIMARY KEY, type TEXT NOT NULL, canonical_name TEXT NOT NULL,
  resident_uid TEXT NOT NULL, source TEXT NOT NULL, content_hash TEXT NOT NULL,
  updated TEXT NOT NULL DEFAULT (datetime('now')));
CREATE TABLE entity_aliases (
  entity_id TEXT NOT NULL, alias TEXT NOT NULL, PRIMARY KEY (entity_id, alias));
CREATE TABLE facts (
  id TEXT PRIMARY KEY, subject_entity_id TEXT, resident_uid TEXT NOT NULL,
  predicate TEXT NOT NULL, value TEXT NOT NULL, confidence REAL,
  source TEXT NOT NULL, timestamp TEXT NOT NULL DEFAULT (datetime('now')));
"""


def _doc(conn, eid, title, resident, facts):
    conn.execute(
        "INSERT INTO entities (id, type, canonical_name, resident_uid, source,"
        " content_hash) VALUES (?, 'document', ?, ?, 'documents', 'h')",
        (eid, title, resident),
    )
    for i, (pred, val, conf, src) in enumerate(facts):
        conn.execute(
            "INSERT INTO facts (id, subject_entity_id, resident_uid, predicate,"
            " value, confidence, source) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (f"{eid}-{i}", eid, resident, pred, val, conf, src),
        )


def _seed(tmp_path):
    db = str(tmp_path / "solaris.db")
    conn = sqlite3.connect(db)
    conn.executescript(_SCHEMA)
    _doc(
        conn,
        "d-ins",
        "ERGO Rechtsschutz",
        "household",
        [
            ("category", "insurance", 0.6, "documents"),
            ("provider", "ERGO", 0.6, "documents"),
            ("cancellation_deadline", "2026-12-15", 0.6, "documents"),
            # a human-confirmed correction of the deadline
            ("cancellation_deadline", "2026-11-30", 1.0, "documents:confirmed"),
        ],
    )
    _doc(
        conn,
        "d-emp",
        "Arbeitsvertrag ACME",
        "mdopp",
        [("category", "employment", 0.6, "documents")],
    )
    _doc(
        conn,
        "d-lena",
        "Lena Hausrat",
        "lena",
        [("category", "insurance", 0.6, "documents")],
    )
    conn.commit()
    conn.close()
    return db


def test_categories_owner_scoped(tmp_path):
    db = _seed(tmp_path)
    # mdopp sees the shared insurance + own employment, NOT lena's private insurance.
    assert documents_portal_db.categories(db, "mdopp") == {
        "insurance": 1,
        "employment": 1,
    }
    # lena sees the shared insurance + her own (2 insurances), no employment.
    assert documents_portal_db.categories(db, "lena") == {"insurance": 2}


def test_category_view_confirmed_value_wins(tmp_path):
    db = _seed(tmp_path)
    rows = documents_portal_db.category_view(db, "mdopp", "insurance")
    assert len(rows) == 1
    row = rows[0]
    assert row["title"] == "ERGO Rechtsschutz"
    assert row["facts"]["provider"]["value"] == "ERGO"
    # The confirmed 1.0 deadline outranks the extracted 0.6 one.
    dl = row["facts"]["cancellation_deadline"]
    assert dl == {"value": "2026-11-30", "confidence": 1.0}


def test_category_view_excludes_other_residents(tmp_path):
    db = _seed(tmp_path)
    # mdopp's insurance view has only the shared ERGO doc, not lena's.
    titles = {
        r["title"] for r in documents_portal_db.category_view(db, "mdopp", "insurance")
    }
    assert titles == {"ERGO Rechtsschutz"}


def test_missing_projection_returns_none(tmp_path):
    assert documents_portal_db.categories(str(tmp_path / "nope.db"), "mdopp") is None


# --- correction endpoint -----------------------------------------------------


class _FakeEngine:
    async def dispatch_tool(self, name, arguments):  # pragma: no cover - unused
        return "{}"


def _app(db):
    from solaris_chat.server import build_app

    return build_app(
        engine=_FakeEngine(),
        remote_user_header="Remote-User",
        default_uid="household",
        solaris_db_path=db,
        notes_dir=str(db) + "-notes",
    )


async def test_correct_endpoint_writes_confirmed_fact(aiohttp_client, tmp_path):
    from solaris_chat.engine.knowledge import projection

    db = _seed(tmp_path)
    client = await aiohttp_client(_app(db))
    r = await client.post(
        "/api/portal/documents/correct",
        json={"entity_id": "d-ins", "predicate": "provider", "value": "ERGO Group"},
        headers={"Remote-User": "mdopp"},
    )
    assert r.status == 200 and (await r.json())["ok"] is True
    conn = projection.open_conn(db)
    try:
        row = conn.execute(
            "SELECT value, confidence FROM facts WHERE subject_entity_id = 'd-ins'"
            " AND predicate = 'provider' AND source = 'documents:confirmed'"
        ).fetchone()
    finally:
        conn.close()
    assert (row["value"], row["confidence"]) == ("ERGO Group", 1.0)


async def test_correct_endpoint_rejects_other_residents_document(
    aiohttp_client, tmp_path
):
    db = _seed(tmp_path)
    client = await aiohttp_client(_app(db))
    # mdopp may not correct lena's private document.
    r = await client.post(
        "/api/portal/documents/correct",
        json={"entity_id": "d-lena", "predicate": "provider", "value": "X"},
        headers={"Remote-User": "mdopp"},
    )
    assert r.status == 404

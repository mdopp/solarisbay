"""Document deadlines → the Solaris Fristen calendar (#doc-graph, slice 3).

`document_deadlines_sync.sync_deadlines` reads a document's dated facts and PUTs
one all-day, alarmed calendar object per deadline into the dedicated `solaris`
account's CalDAV collection (authenticated HTTP). These tests mock `put_item`
and prove the event/alarm content, the stable overwrite UID, that non-deadline
/ unparseable dates are skipped, and the disabled no-op.
"""

from __future__ import annotations

import sqlite3

from solaris_chat.engine import document_deadlines_sync
from solaris_chat.engine.document_deadlines_sync import sync_deadlines

_SCHEMA = """
CREATE TABLE entities (
  id TEXT PRIMARY KEY, type TEXT NOT NULL, canonical_name TEXT NOT NULL,
  resident_uid TEXT NOT NULL, source TEXT NOT NULL, content_hash TEXT NOT NULL,
  updated TEXT NOT NULL DEFAULT (datetime('now')));
CREATE TABLE facts (
  id TEXT PRIMARY KEY, subject_entity_id TEXT, resident_uid TEXT NOT NULL,
  predicate TEXT NOT NULL, value TEXT NOT NULL, confidence REAL,
  source TEXT NOT NULL, timestamp TEXT NOT NULL DEFAULT (datetime('now')));
"""

_URL = "https://caldav.example/solaris/fristen/"


def _doc(conn, eid, title, facts):
    conn.execute(
        "INSERT INTO entities (id, type, canonical_name, resident_uid, source,"
        " content_hash) VALUES (?, 'document', ?, 'mdopp', 'documents', 'h')",
        (eid, title),
    )
    for i, (pred, val) in enumerate(facts):
        conn.execute(
            "INSERT INTO facts (id, subject_entity_id, resident_uid, predicate,"
            " value, confidence, source) VALUES (?, ?, 'mdopp', ?, ?, 0.6, 'd')",
            (f"{eid}-{i}", eid, pred, val),
        )


def _db(tmp_path, facts):
    db = str(tmp_path / "solaris.db")
    conn = sqlite3.connect(db)
    conn.executescript(_SCHEMA)
    _doc(conn, "d1", "ERGO Rechtsschutz", facts)
    conn.commit()
    conn.close()
    return db


def _capture(monkeypatch):
    calls = []

    async def fake_put(self, collection_url, uid, body, *, suffix, content_type):
        calls.append(
            {"url": collection_url, "uid": uid, "body": body, "suffix": suffix}
        )
        return collection_url + uid

    monkeypatch.setattr(document_deadlines_sync.HttpDavClient, "put_item", fake_put)
    return calls


async def test_deadline_puts_alarmed_all_day_event(tmp_path, monkeypatch):
    calls = _capture(monkeypatch)
    db = _db(
        tmp_path,
        [
            ("provider", "ERGO"),
            ("policy_number", "SV 072714970"),
            ("cancellation_deadline", "2026-12-15"),
        ],
    )
    out = await sync_deadlines(db, _URL, "solaris", "pw")
    assert out == {"written": 1, "skipped": 0, "failed": 0}
    c = calls[0]
    assert c["url"] == _URL and c["suffix"] == ".ics"
    assert c["uid"] == "solaris-deadline-d1-cancellation_deadline"
    assert "SUMMARY:Kündigungsfrist: ERGO Rechtsschutz" in c["body"]
    assert "DTSTART;VALUE=DATE:20261215" in c["body"]
    assert "BEGIN:VALARM" in c["body"] and "TRIGGER:-P14D" in c["body"]
    assert "ERGO" in c["body"] and "SV 072714970" in c["body"]


async def test_multiple_deadlines_each_put(tmp_path, monkeypatch):
    calls = _capture(monkeypatch)
    db = _db(
        tmp_path,
        [("cancellation_deadline", "2026-12-15"), ("hu_date", "2027-03-01")],
    )
    out = await sync_deadlines(db, _URL, "solaris", "pw")
    assert out["written"] == 2 and len(calls) == 2


async def test_unparseable_date_skipped(tmp_path, monkeypatch):
    calls = _capture(monkeypatch)
    db = _db(tmp_path, [("cancellation_deadline", "zum Vertragsende")])
    out = await sync_deadlines(db, _URL, "solaris", "pw")
    assert out == {"written": 0, "skipped": 1, "failed": 0}
    assert calls == []


async def test_non_deadline_date_ignored(tmp_path, monkeypatch):
    calls = _capture(monkeypatch)
    db = _db(tmp_path, [("start_date", "2026-01-01")])
    out = await sync_deadlines(db, _URL, "solaris", "pw")
    assert out == {"written": 0, "skipped": 0, "failed": 0}
    assert calls == []


async def test_disabled_when_unconfigured(tmp_path, monkeypatch):
    calls = _capture(monkeypatch)
    db = _db(tmp_path, [("cancellation_deadline", "2026-12-15")])
    assert await sync_deadlines(db, "", "solaris", "pw") == {
        "written": 0,
        "skipped": 0,
        "failed": 0,
    }
    assert calls == []

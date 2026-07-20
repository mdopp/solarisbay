"""Document deadlines → the "Solaris Fristen" calendar (#doc-graph, slice 3).

`document_deadlines_sync.sync_deadlines` reads a document's dated facts and drops
one all-day, alarmed `<uid>.ics` per deadline into the owner's Radicale calendar.
These tests prove the write target, the event/alarm content, the stable
overwrite UID, that non-deadline/unparseable dates are skipped, the household
skip, and the disabled no-op.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from solaris_chat.engine import document_deadlines_sync

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


def _doc(conn, eid, title, resident, facts):
    conn.execute(
        "INSERT INTO entities (id, type, canonical_name, resident_uid, source,"
        " content_hash) VALUES (?, 'document', ?, ?, 'documents', 'h')",
        (eid, title, resident),
    )
    for i, (pred, val) in enumerate(facts):
        conn.execute(
            "INSERT INTO facts (id, subject_entity_id, resident_uid, predicate,"
            " value, confidence, source) VALUES (?, ?, ?, ?, ?, 0.6, 'documents')",
            (f"{eid}-{i}", eid, resident, pred, val),
        )


def _db(tmp_path, facts, resident="mdopp"):
    db = str(tmp_path / "solaris.db")
    conn = sqlite3.connect(db)
    conn.executescript(_SCHEMA)
    _doc(conn, "d1", "ERGO Rechtsschutz", resident, facts)
    conn.commit()
    conn.close()
    return db


def _events(radicale_data, user):
    coll = (
        Path(radicale_data)
        / "collections"
        / "collection-root"
        / user
        / "solaris-fristen"
    )
    return sorted(coll.glob("*.ics")) if coll.exists() else []


def test_deadline_becomes_alarmed_all_day_event(tmp_path):
    db = _db(
        tmp_path,
        [
            ("provider", "ERGO"),
            ("policy_number", "SV 072714970"),
            ("cancellation_deadline", "2026-12-15"),
        ],
    )
    rad = str(tmp_path / "radicale")
    out = document_deadlines_sync.sync_deadlines(db, rad)
    assert out == {"written": 1, "skipped": 0}
    events = _events(rad, "mdopp")
    assert len(events) == 1
    ics = events[0].read_text()
    assert "SUMMARY:Kündigungsfrist: ERGO Rechtsschutz" in ics
    assert "DTSTART;VALUE=DATE:20261215" in ics
    assert "BEGIN:VALARM" in ics and "TRIGGER:-P14D" in ics
    assert "ERGO" in ics and "SV 072714970" in ics  # description
    assert "UID:solaris-deadline-d1-cancellation_deadline" in ics


def test_multiple_deadlines_one_event_each(tmp_path):
    db = _db(
        tmp_path,
        [("cancellation_deadline", "2026-12-15"), ("hu_date", "2027-03-01")],
    )
    rad = str(tmp_path / "radicale")
    out = document_deadlines_sync.sync_deadlines(db, rad)
    assert out["written"] == 2
    assert len(_events(rad, "mdopp")) == 2


def test_idempotent_overwrite(tmp_path):
    db = _db(tmp_path, [("cancellation_deadline", "2026-12-15")])
    rad = str(tmp_path / "radicale")
    document_deadlines_sync.sync_deadlines(db, rad)
    document_deadlines_sync.sync_deadlines(db, rad)
    assert len(_events(rad, "mdopp")) == 1  # stable UID → overwrite


def test_unparseable_date_skipped(tmp_path):
    # "zum Ende des Versicherungsjahres" isn't ISO — counted as skipped, no event.
    db = _db(tmp_path, [("cancellation_deadline", "zum Vertragsende")])
    rad = str(tmp_path / "radicale")
    out = document_deadlines_sync.sync_deadlines(db, rad)
    assert out == {"written": 0, "skipped": 1}
    assert _events(rad, "mdopp") == []


def test_non_deadline_date_ignored(tmp_path):
    # start_date is a date but NOT a deadline — no event, not even a skip.
    db = _db(tmp_path, [("start_date", "2026-01-01")])
    rad = str(tmp_path / "radicale")
    assert document_deadlines_sync.sync_deadlines(db, rad) == {
        "written": 0,
        "skipped": 0,
    }


def test_household_doc_skipped(tmp_path):
    db = _db(tmp_path, [("cancellation_deadline", "2026-12-15")], resident="household")
    out = document_deadlines_sync.sync_deadlines(db, str(tmp_path / "radicale"))
    assert out == {"written": 0, "skipped": 0}


def test_disabled_when_no_radicale_data(tmp_path):
    db = _db(tmp_path, [("cancellation_deadline", "2026-12-15")])
    assert document_deadlines_sync.sync_deadlines(db, "") == {
        "written": 0,
        "skipped": 0,
    }

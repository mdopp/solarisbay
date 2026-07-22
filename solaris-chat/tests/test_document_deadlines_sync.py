"""Document deadlines + dated tasks → PER-RESIDENT Solaris calendars (#doc-graph,
#997).

`document_deadlines_sync.sync_deadlines` reads each document's dated facts and
each resident's dated OPEN tasks and PUTs one all-day, alarmed calendar object
per item into a `solaris` calendar under the owner's OWN principal
(`{base}/{uid}/solaris/`, option A / #1011). These
tests mock `put_item`/`ensure_calendar` and prove the per-resident collection
URLs, the event/alarm content, the stable overwrite UID, owner-scoping (a
resident's task never reaches another's calendar), that non-deadline /
unparseable dates are skipped, and the disabled no-op.
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

_BASE = "https://caldav.example/dav"
_SHARED = document_deadlines_sync.projection.SHARED_UID
_CALENDAR = document_deadlines_sync._CALENDAR
# Document deadlines are household data → the `solaris` calendar under the
# household principal (option A / #1011).
_SHARED_URL = f"{_BASE}/{_SHARED}/{_CALENDAR}/"


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


def _task(conn, eid, title, resident_uid, facts):
    conn.execute(
        "INSERT INTO entities (id, type, canonical_name, resident_uid, source,"
        " content_hash) VALUES (?, 'task', ?, ?, 'todo', 'h')",
        (eid, title, resident_uid),
    )
    for i, (pred, val) in enumerate(facts):
        conn.execute(
            "INSERT INTO facts (id, subject_entity_id, resident_uid, predicate,"
            " value, confidence, source) VALUES (?, ?, ?, ?, ?, 0.6, 't')",
            (f"{eid}-{i}", eid, resident_uid, pred, val),
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
    ensured = []

    async def fake_put(self, collection_url, uid, body, *, suffix, content_type):
        calls.append(
            {"url": collection_url, "uid": uid, "body": body, "suffix": suffix}
        )
        return collection_url + uid

    async def fake_ensure(self, collection_url):
        ensured.append(collection_url)

    monkeypatch.setattr(document_deadlines_sync.HttpDavClient, "put_item", fake_put)
    monkeypatch.setattr(
        document_deadlines_sync.HttpDavClient, "ensure_calendar", fake_ensure
    )
    return calls, ensured


async def test_deadline_puts_alarmed_all_day_event(tmp_path, monkeypatch):
    calls, ensured = _capture(monkeypatch)
    db = _db(
        tmp_path,
        [
            ("provider", "ERGO"),
            ("policy_number", "SV 072714970"),
            ("cancellation_deadline", "2026-12-15"),
        ],
    )
    out = await sync_deadlines(db, _BASE, "solaris", "pw")
    assert out == {"written": 1, "skipped": 0, "failed": 0}
    c = calls[0]
    # A document deadline lands in the household principal's calendar.
    assert c["url"] == _SHARED_URL and c["suffix"] == ".ics"
    assert _SHARED_URL in ensured  # collection ensured before its PUTs
    assert c["uid"] == "solaris-deadline-d1-cancellation_deadline"
    assert "SUMMARY:Kündigungsfrist: ERGO Rechtsschutz" in c["body"]
    assert "DTSTART;VALUE=DATE:20261215" in c["body"]
    assert "BEGIN:VALARM" in c["body"] and "TRIGGER:-P14D" in c["body"]
    assert "ERGO" in c["body"] and "SV 072714970" in c["body"]


async def test_multiple_deadlines_each_put(tmp_path, monkeypatch):
    calls, _ = _capture(monkeypatch)
    db = _db(
        tmp_path,
        [("cancellation_deadline", "2026-12-15"), ("hu_date", "2027-03-01")],
    )
    out = await sync_deadlines(db, _BASE, "solaris", "pw")
    assert out["written"] == 2 and len(calls) == 2


async def test_unparseable_date_skipped(tmp_path, monkeypatch):
    calls, _ = _capture(monkeypatch)
    db = _db(tmp_path, [("cancellation_deadline", "zum Vertragsende")])
    out = await sync_deadlines(db, _BASE, "solaris", "pw")
    assert out == {"written": 0, "skipped": 1, "failed": 0}
    assert calls == []


async def test_non_deadline_date_ignored(tmp_path, monkeypatch):
    calls, _ = _capture(monkeypatch)
    db = _db(tmp_path, [("start_date", "2026-01-01")])
    out = await sync_deadlines(db, _BASE, "solaris", "pw")
    assert out == {"written": 0, "skipped": 0, "failed": 0}
    assert calls == []


async def test_task_lands_in_owning_residents_calendar(tmp_path, monkeypatch):
    # #997 owner-scoping: a resident's private dated task is written ONLY under
    # that resident's own DAV context, and a household task under SHARED_UID —
    # a resident's task must never reach another resident's calendar.
    calls, ensured = _capture(monkeypatch)
    db = str(tmp_path / "solaris.db")
    conn = sqlite3.connect(db)
    conn.executescript(_SCHEMA)
    _task(
        conn,
        "t-lena",
        "Gynäkologe",
        "lena",
        [("status", "open"), ("due", "2026-09-01"), ("title_text", "Gynäkologe")],
    )
    _task(
        conn,
        "t-shared",
        "Müll rausbringen",
        _SHARED,
        [("status", "open"), ("due", "2026-09-02"), ("title_text", "Müll rausbringen")],
    )
    conn.commit()
    conn.close()
    out = await sync_deadlines(db, _BASE, "solaris", "pw")
    assert out == {"written": 2, "skipped": 0, "failed": 0}
    by_uid = {c["uid"]: c for c in calls}
    lena_url = f"{_BASE}/lena/{_CALENDAR}/"
    # Lena's task → only Lena's calendar; the household task → only SHARED.
    assert by_uid["solaris-task-t-lena"]["url"] == lena_url
    assert by_uid["solaris-task-t-shared"]["url"] == _SHARED_URL
    # Owner-safety: Lena's task never PUT to the household (B's) URL, and vice versa.
    assert not any(
        c["uid"] == "solaris-task-t-lena" and c["url"] == _SHARED_URL for c in calls
    )
    assert not any(
        c["uid"] == "solaris-task-t-shared" and c["url"] == lena_url for c in calls
    )
    # Each owner's collection ensured before its PUTs.
    assert lena_url in ensured and _SHARED_URL in ensured
    # And Lena's private title never leaks into the household calendar's bodies.
    assert all("Gynäkologe" not in c["body"] for c in calls if c["url"] == _SHARED_URL)


async def test_household_items_routed_to_primary_resident(tmp_path, monkeypatch):
    # #1011: `household` isn't a real Radicale principal (writing under it 409s),
    # so with a configured primary resident the household document deadline AND a
    # household task are routed to THAT resident's own `/uid/solaris/` calendar.
    calls, ensured = _capture(monkeypatch)
    db = str(tmp_path / "solaris.db")
    conn = sqlite3.connect(db)
    conn.executescript(_SCHEMA)
    _doc(conn, "d1", "ERGO Rechtsschutz", [("cancellation_deadline", "2026-12-15")])
    _task(
        conn,
        "t-shared",
        "Müll rausbringen",
        _SHARED,
        [("status", "open"), ("due", "2026-09-02"), ("title_text", "Müll rausbringen")],
    )
    conn.commit()
    conn.close()
    out = await sync_deadlines(db, _BASE, "solaris", "pw", household_uid="mdopp")
    assert out == {"written": 2, "skipped": 0, "failed": 0}
    mdopp_url = f"{_BASE}/mdopp/{_CALENDAR}/"
    # Both the household deadline and the household task land on mdopp's calendar,
    # and nothing is written under the principal-less `household` uid.
    assert all(c["url"] == mdopp_url for c in calls)
    assert _SHARED_URL not in ensured
    assert {c["uid"] for c in calls} == {
        "solaris-deadline-d1-cancellation_deadline",
        "solaris-task-t-shared",
    }


async def test_resolved_task_not_written(tmp_path, monkeypatch):
    calls, _ = _capture(monkeypatch)
    db = str(tmp_path / "solaris.db")
    conn = sqlite3.connect(db)
    conn.executescript(_SCHEMA)
    _task(
        conn,
        "t-done",
        "erledigt",
        "lena",
        [("status", "done"), ("due", "2026-09-01"), ("title_text", "erledigt")],
    )
    conn.commit()
    conn.close()
    out = await sync_deadlines(db, _BASE, "solaris", "pw")
    assert out == {"written": 0, "skipped": 0, "failed": 0}
    assert calls == []


async def test_disabled_when_unconfigured(tmp_path, monkeypatch):
    calls, ensured = _capture(monkeypatch)
    db = _db(tmp_path, [("cancellation_deadline", "2026-12-15")])
    assert await sync_deadlines(db, "", "solaris", "pw") == {
        "written": 0,
        "skipped": 0,
        "failed": 0,
    }
    assert calls == [] and ensured == []

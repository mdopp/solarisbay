"""CalDAV PUT write path: the calendar importer PUTs each event group to a
Radicale collection over HTTP, with the aiohttp session mocked (no network)."""

from __future__ import annotations

from solaris_chat.engine.importers import google_takeout as gt
from solaris_chat.engine.importers.google_takeout.importers import calendar as cal
from solaris_chat.engine.ingest import dav_client
from solaris_chat.engine.ingest.dav_client import HttpDavClient

ICS = b"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Google//EN
BEGIN:VTIMEZONE
TZID:Europe/Berlin
BEGIN:STANDARD
DTSTART:19701025T030000
TZOFFSETFROM:+0200
TZOFFSETTO:+0100
END:STANDARD
END:VTIMEZONE
BEGIN:VEVENT
UID:e1@g
DTSTART;TZID=Europe/Berlin:20260101T100000
SUMMARY:Serie
RRULE:FREQ=WEEKLY;COUNT=3
END:VEVENT
BEGIN:VEVENT
UID:e1@g
RECURRENCE-ID;TZID=Europe/Berlin:20260108T100000
DTSTART;TZID=Europe/Berlin:20260108T120000
SUMMARY:Serie verschoben
END:VEVENT
BEGIN:VEVENT
UID:e2/slash@g
DTSTART;VALUE=DATE:20260214
SUMMARY:Tag
END:VEVENT
END:VCALENDAR
"""


class _FakeResp:
    def __init__(self):
        self.raised = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def raise_for_status(self):
        self.raised = True


class _FakeSession:
    """Records each PUT (url, data, headers); no real network."""

    puts: list[tuple[str, bytes, dict]] = []

    def __init__(self, *a, **k):
        self.auth = k.get("auth")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def put(self, url, data=None, headers=None):
        _FakeSession.puts.append((url, data, headers or {}))
        return _FakeResp()


def _patch_session(monkeypatch):
    _FakeSession.puts = []
    monkeypatch.setattr(dav_client.aiohttp, "ClientSession", _FakeSession)


async def test_put_item_uses_uid_href_and_caldav_content_type(monkeypatch):
    _patch_session(monkeypatch)
    client = HttpDavClient(caldav_username="u", caldav_password="p")
    url = await client.put_item("https://dav/u/cal", "e1@g", "BEGIN:VCALENDAR\r\n")
    assert url == "https://dav/u/cal/e1-g.ics"  # unsafe UID chars sanitized
    (put_url, data, headers) = _FakeSession.puts[0]
    assert put_url == "https://dav/u/cal/e1-g.ics"
    assert data == b"BEGIN:VCALENDAR\r\n"
    assert headers["Content-Type"].startswith("text/calendar")


async def test_import_to_dav_puts_one_object_per_uid_group(monkeypatch):
    _patch_session(monkeypatch)
    client = HttpDavClient(caldav_username="u", caldav_password="p")
    rep = await cal.import_to_dav(client, "https://dav/u/Privat/", "Privat.ics", ICS)
    assert rep["objects"] == 3
    assert rep["written"] == 2  # grouped by UID (e1@g override merged)
    assert rep["target"] == "https://dav/u/Privat/"
    assert len(_FakeSession.puts) == 2
    # The two same-UID components ride one VCALENDAR PUT.
    merged = [d for (_u, d, _h) in _FakeSession.puts if d.count(b"BEGIN:VEVENT") == 2]
    assert len(merged) == 1


async def test_calendar_kind_registered_and_run_writes_via_dav(monkeypatch):
    _patch_session(monkeypatch)
    imp = gt.get("calendar")
    assert imp is not None
    assert isinstance(imp, gt.Importer)  # satisfies the runtime-checkable protocol
    plan = imp.plan({"filename": "Privat.ics", "ics_bytes": ICS}, None)
    assert plan.kind == "calendar"
    assert plan.summary["items"] == 2
    client = HttpDavClient(caldav_username="u", caldav_password="p")
    reports = await imp.run(
        plan, {"client": client, "collection_url": "https://dav/u/Privat/"}
    )
    assert reports[0]["written"] == 2
    assert len(_FakeSession.puts) == 2

from solaris_chat.engine.importers.google_takeout.importers import calendar as cal

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
UID:e2@g
DTSTART;VALUE=DATE:20260214
SUMMARY:Tag
END:VEVENT
END:VCALENDAR
"""


def _user_root(paths, user):
    return paths.radicale_data / "collections" / "collection-root" / user


def test_calendar_name_strip():
    assert cal._calendar_name("Persönlich.ics") == "Persönlich"
    assert cal._calendar_name("x/y/Cal.ical") == "Cal"
    assert cal._calendar_name("") == "Kalender"


def test_preview_counts():
    p = cal.preview("Cal.ics", ICS)
    assert p["objects"] == 3
    assert p["items"] == 2  # grouped by UID
    assert p["timezones"] == 1


def test_import_groups_by_uid_and_merges_override(paths):
    rep = cal.do_import(paths.radicale_data, "calu1", "Privat.ics", ICS)
    assert rep["written"] == 2
    coll = _user_root(paths, "calu1") / "Privat"
    files = list(coll.glob("*.ics"))
    assert len(files) == 2
    merged = [f for f in files if f.read_text().count("BEGIN:VEVENT") == 2]
    assert len(merged) == 1  # the two same-UID components share one file

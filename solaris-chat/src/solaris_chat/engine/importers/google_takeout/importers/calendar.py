"""Import Google Calendar ``.ics`` exports into the acting user's Radicale
calendar collection.

Google exports one ``.ics`` per calendar (``Takeout/Calendar/<Name>.ics``), each
a single ``VCALENDAR`` holding many ``VEVENT``s plus the ``VTIMEZONE``s they
reference. Radicale's storage wants one item file per calendar object, each a
self-contained ``VCALENDAR``. So we split by ``UID`` (keeping recurrence
overrides that share a UID together) and wrap each group in its own VCALENDAR
carrying the source's timezones.

Idempotent: the item filename is derived from the UID, so re-importing the same
export overwrites rather than duplicating.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from pathlib import Path

from icalendar import Calendar

from .. import radicale_store

_PRODID = "-//solaris-import-google//Calendar//EN"
_NS = uuid.UUID("6f1a1c2e-9b1e-4b7a-9c2d-000000000001")

# Component types Radicale stores as calendar objects.
_OBJECT_TYPES = ("VEVENT", "VTODO", "VJOURNAL")


def _uid_for(component) -> str:
    uid = component.get("UID")
    if uid:
        return str(uid)
    # Stable synthetic UID from summary + start so re-imports stay idempotent.
    basis = f"{component.get('SUMMARY', '')}|{component.get('DTSTART', '')}"
    return f"import-{uuid.uuid5(_NS, basis)}"


def _parse(ics_bytes: bytes):
    """Return (groups: uid->[components], timezones: [VTIMEZONE], total_objects)."""
    cal = Calendar.from_ical(ics_bytes)
    timezones = [c for c in cal.walk("VTIMEZONE")]
    groups: dict[str, list] = defaultdict(list)
    total = 0
    for comp in cal.walk():
        if comp.name in _OBJECT_TYPES:
            total += 1
            groups[_uid_for(comp)].append(comp)
    return groups, timezones, total


def _build_item(components, timezones) -> str:
    cal = Calendar()
    cal.add("prodid", _PRODID)
    cal.add("version", "2.0")
    for tz in timezones:
        cal.add_component(tz)
    for comp in components:
        cal.add_component(comp)
    return cal.to_ical().decode("utf-8")


def _summaries(groups, limit=5):
    out = []
    for comps in list(groups.values())[:limit]:
        c = comps[0]
        summary = str(c.get("SUMMARY", "(ohne Titel)"))
        start = c.get("DTSTART")
        out.append({"summary": summary, "start": str(start.dt) if start else ""})
    return out


def preview(filename: str, ics_bytes: bytes) -> dict:
    groups, timezones, total = _parse(ics_bytes)
    return {
        "type": "calendar",
        "calendar_name": _calendar_name(filename),
        "objects": total,
        "items": len(groups),
        "timezones": len(timezones),
        "samples": _summaries(groups),
    }


def do_import(radicale_data: Path, user: str, filename: str, ics_bytes: bytes) -> dict:
    groups, timezones, total = _parse(ics_bytes)
    name = _calendar_name(filename)
    written = 0
    with radicale_store.storage_lock(radicale_data):
        coll = radicale_store.ensure_collection(
            radicale_data, user, name, tag="VCALENDAR", displayname=name
        )
        for uid, comps in groups.items():
            radicale_store.write_item(coll, uid, _build_item(comps, timezones), "ics")
            written += 1
    return {
        "type": "calendar",
        "calendar_name": name,
        "objects": total,
        "written": written,
        "target": f"{user}/{radicale_store.sanitize_name(name)}",
    }


def _calendar_name(filename: str) -> str:
    base = (filename or "Kalender").rsplit("/", 1)[-1]
    for suffix in (".ical", ".ics"):
        if base.lower().endswith(suffix):
            base = base[: -len(suffix)]
    return base.strip() or "Kalender"

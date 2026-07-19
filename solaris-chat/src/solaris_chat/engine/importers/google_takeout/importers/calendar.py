"""Import Google Calendar ``.ics`` exports into the acting user's Radicale
calendar collection over CalDAV.

Google exports one ``.ics`` per calendar (``Takeout/Calendar/<Name>.ics``), each
a single ``VCALENDAR`` holding many ``VEVENT``s plus the ``VTIMEZONE``s they
reference. Radicale's storage wants one item file per calendar object, each a
self-contained ``VCALENDAR``. So we split by ``UID`` (keeping recurrence
overrides that share a UID together) and wrap each group in its own VCALENDAR
carrying the source's timezones, then ``PUT`` each group to the owner's Radicale
collection over CalDAV (RFC 4791/4918) rather than writing Radicale's on-disk
storage — a plain authenticated write that avoids the Radicale userns-uid caveat
(servicebay#2344/#2345). The written events are projected to OKF on the next
nightly ``DavIngest`` run (``source="caldav"``); no new ingest code is added.

Idempotent: the resource href is derived from the UID, so re-importing the same
export overwrites rather than duplicating.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from pathlib import Path

from icalendar import Calendar

from ....ingest.dav_client import HttpDavClient
from .. import ImportPlan, radicale_store

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


async def import_to_dav(
    client: HttpDavClient, collection_url: str, filename: str, ics_bytes: bytes
) -> dict:
    """PUT each event group to a Radicale CalDAV collection over HTTP.

    ``collection_url`` is the owner's calendar collection URL (e.g.
    ``https://…/dav/<user>/<calendar>/``); each UID-group is wrapped in its own
    VCALENDAR and PUT as ``<uid>.ics``. Returns the same report shape as
    ``do_import`` so the two write paths are interchangeable.
    """
    groups, timezones, total = _parse(ics_bytes)
    name = _calendar_name(filename)
    written = 0
    for uid, comps in groups.items():
        await client.put_item(collection_url, uid, _build_item(comps, timezones))
        written += 1
    return {
        "type": "calendar",
        "calendar_name": name,
        "objects": total,
        "written": written,
        "target": collection_url,
    }


def _calendar_name(filename: str) -> str:
    base = (filename or "Kalender").rsplit("/", 1)[-1]
    for suffix in (".ical", ".ics"):
        if base.lower().endswith(suffix):
            base = base[: -len(suffix)]
    return base.strip() or "Kalender"


class CalendarImporter:
    """Registrable ``calendar`` importer kind.

    Parses a Takeout ``.ics`` (``plan``) and PUTs each event to the owner's
    Radicale CalDAV collection (``run``); ``DavIngest`` projects them to OKF on
    the next nightly run. The heavy detect/plan/run job wiring (upload manifest,
    progress) lands with the durable runner; ``run`` here carries out the CalDAV
    write given a client + collection URL supplied in the plan.
    """

    kind = "calendar"

    def detect(self, manifest) -> list[dict]:
        return [{"kind": self.kind, "type": "calendar"}]

    def plan(self, archive, selections) -> ImportPlan:
        ics_bytes = archive["ics_bytes"]
        filename = archive.get("filename", "")
        return ImportPlan(
            kind=self.kind,
            writes=[{"filename": filename, "ics_bytes": ics_bytes}],
            summary=preview(filename, ics_bytes),
        )

    async def run(self, plan: ImportPlan, progress) -> list[dict]:
        client: HttpDavClient = progress["client"]
        collection_url: str = progress["collection_url"]
        reports = []
        for write in plan.writes:
            reports.append(
                await import_to_dav(
                    client, collection_url, write["filename"], write["ics_bytes"]
                )
            )
        return reports

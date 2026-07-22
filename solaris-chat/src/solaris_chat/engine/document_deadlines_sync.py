"""Project document deadlines + dated to-do tasks into PER-RESIDENT calendars
(#doc-graph / #todo / #997).

A document carries dated facts — a `cancellation_deadline`, a `hu_date`, a
contract `end_date`. Rather than push notifications, we write each as an all-day
event with an advance alarm into a calendar, where the calendar app itself
reminds the resident (the passive, non-intrusive channel the plan chose).

Solaris owns, the calendar mirrors (#997): each resident's dated OPEN tasks are
written under THAT resident's own DAV context — `{base}/{resident_uid}/<cal>/` —
so a resident's private task lands only in their own calendar and is never
visible cross-resident. Household-scoped document deadlines and household tasks
(`SHARED_UID`) go to the household principal's calendar
`{base}/{SHARED_UID}/<cal>/`.

**Authenticated CalDAV PUT** as the dedicated `solaris` DAV account, not a
filesystem mount: Radicale's rights scope it to the collections it may write.
The event UID is derived from `(entity_id, predicate)` / the task id, so a
re-sync overwrites in place. Non-ISO / unparseable dates are skipped. Disabled
(no-op) when the base URL / credentials are unset.
"""

from __future__ import annotations

from datetime import date, timedelta

from icalendar import Alarm, Calendar, Event

from solaris_chat.engine.ingest.dav_client import HttpDavClient
from solaris_chat.engine.knowledge import projection
from solaris_chat.logging import log

# The dated predicates that become calendar entries, with the German label the
# event summary uses. `start_date` is deliberately absent — a contract's start is
# not a deadline to be reminded about.
_DEADLINE_LABELS = {
    "cancellation_deadline": "Kündigungsfrist",
    "renewal_date": "Verlängerung",
    "hu_date": "TÜV/HU",
    "expiry_date": "Ablauf",
    "due_date": "Fällig",
    "end_date": "Vertragsende",
}
_UID_PREFIX = "solaris-deadline-"
_TASK_UID_PREFIX = "solaris-task-"
# Days before the date the calendar app raises its alarm.
_LEAD_DAYS = 14
# The stable Solaris-owned calendar name each resident's collection lives under:
# `{base}/{resident_uid}/{_CALENDAR_NAME}/`. Named for the surface it backs
# (Aufgaben/To-Do + Fristen) and kept stable so a re-sync overwrites in place.
_CALENDAR_NAME = "solaris"


def _parse_iso(value: str) -> date | None:
    try:
        return date.fromisoformat(value.strip())
    except (ValueError, AttributeError):
        return None


def _build_event(uid: str, summary: str, description: str, day: date) -> str:
    cal = Calendar()
    cal.add("prodid", "-//solaris//deadlines//EN")
    cal.add("version", "2.0")
    ev = Event()
    ev.add("uid", uid)
    ev.add("summary", summary)
    if description:
        ev.add("description", description)
    ev.add("dtstart", day)  # a date (not datetime) → all-day VALUE=DATE
    ev.add("dtend", day + timedelta(days=1))
    alarm = Alarm()
    alarm.add("action", "DISPLAY")
    alarm.add("description", summary)
    alarm.add("trigger", timedelta(days=-_LEAD_DAYS))
    ev.add_component(alarm)
    cal.add_component(ev)
    return cal.to_ical().decode()


def _facts(conn, entity_id: str) -> dict[str, str]:
    """The document's facts, highest-confidence value winning per predicate."""
    out: dict[str, str] = {}
    for f in conn.execute(
        "SELECT predicate, value FROM facts WHERE subject_entity_id = ?"
        " ORDER BY confidence DESC",
        (entity_id,),
    ).fetchall():
        if f["predicate"] not in out:
            out[f["predicate"]] = f["value"]
    return out


def _collection_url(base_url: str, resident_uid: str) -> str:
    """The resident's Solaris calendar collection — `{base}/{uid}/{cal}/`.

    Same URL shape the Takeout calendar importer PUTs to (orchestrator
    `{base}/{owner_uid}/{cal_name}/`), so both write paths agree.
    """
    return f"{base_url.rstrip('/')}/{resident_uid}/{_CALENDAR_NAME}/"


async def sync_deadlines(
    db_path: str, base_url: str, username: str, password: str
) -> dict[str, int]:
    """PUT each resident's dated OPEN tasks + the household's document deadlines
    into their PER-RESIDENT Solaris calendar (`{base}/{resident_uid}/<cal>/`).

    Returns `{written, skipped, failed}`. Never raises. Disabled (no-op) when the
    base URL / credentials are unset.
    """
    if not (base_url and username and password):
        return {"written": 0, "skipped": 0, "failed": 0}
    client = HttpDavClient(caldav_username=username, caldav_password=password)
    written = skipped = failed = 0
    # owner resident_uid → [(uid, ics)]; an event only ever lands under its own
    # owner's key, so a resident's task can never cross into another's calendar.
    per_resident: dict[str, list[tuple[str, str]]] = {}
    conn = projection.open_conn(db_path)
    try:
        # Household document deadlines → the household principal's calendar.
        docs = conn.execute(
            "SELECT id, canonical_name FROM entities WHERE type = 'document'"
        ).fetchall()
        for doc in docs:
            facts = _facts(conn, doc["id"])
            desc = " · ".join(
                x
                for x in (facts.get("provider", ""), facts.get("policy_number", ""))
                if x
            )
            for pred, label in _DEADLINE_LABELS.items():
                raw = facts.get(pred, "")
                if not raw:
                    continue
                day = _parse_iso(raw)
                if day is None:
                    skipped += 1
                    continue
                uid = f"{_UID_PREFIX}{doc['id']}-{pred}"
                summary = f"{label}: {doc['canonical_name']}"
                per_resident.setdefault(projection.SHARED_UID, []).append(
                    (uid, _build_event(uid, summary, desc, day))
                )
        # Dated to-do tasks (#todo, #997): an OPEN task with an ISO `due` becomes
        # a calendar entry in ITS OWNER's calendar — a private resident's task
        # under their own uid, a household task under SHARED_UID. A resolved task
        # simply stops being written (its event lingers until re-synced away —
        # refined by cascade-on-change, see the TODO below).
        tasks = conn.execute(
            "SELECT id, canonical_name, resident_uid FROM entities WHERE type = 'task'"
        ).fetchall()
        for task in tasks:
            facts = _facts(conn, task["id"])
            if facts.get("status", "open") != "open":
                continue
            day = _parse_iso(facts.get("due", ""))
            if day is None:
                continue
            uid = f"{_TASK_UID_PREFIX}{task['id']}"
            summary = f"Aufgabe: {facts.get('title_text', task['canonical_name'])}"
            per_resident.setdefault(task["resident_uid"], []).append(
                (uid, _build_event(uid, summary, "", day))
            )
    finally:
        conn.close()
    # TODO(#997): cascade-on-change — task.add/update/set_status should re-PUT the
    # single affected event immediately, so the calendar isn't stale until night.
    for resident_uid, events in per_resident.items():
        collection_url = _collection_url(base_url, resident_uid)
        try:
            await client.ensure_calendar(collection_url)
        except Exception as e:  # noqa: BLE001 — a missing collection fails its PUTs below.
            log.error(
                "engine.deadlines_sync.ensure_failed",
                resident=resident_uid,
                error=str(e),
            )
        for uid, ics in events:
            try:
                await client.put_item(
                    collection_url,
                    uid,
                    ics,
                    suffix=".ics",
                    content_type="text/calendar; charset=utf-8",
                )
                written += 1
            except Exception as e:  # noqa: BLE001 — one bad event mustn't stop the sync.
                log.error("engine.deadlines_sync.event_failed", uid=uid, error=str(e))
                failed += 1
    log.info("engine.deadlines_sync", written=written, skipped=skipped, failed=failed)
    return {"written": written, "skipped": skipped, "failed": failed}

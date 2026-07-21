"""Project document deadlines + dated to-do tasks into a calendar (#doc-graph /
#todo).

A document carries dated facts — a `cancellation_deadline`, a `hu_date`, a
contract `end_date`. Rather than push notifications, we write each as an all-day
event with an advance alarm into a shared "Solaris Fristen" calendar, where the
calendar app itself reminds the household (the passive, non-intrusive channel the
plan chose) and the layer is toggleable.

**Authenticated CalDAV PUT** as the dedicated `solaris` DAV account, not a
filesystem mount: Radicale's `owner_only` scopes it to its own calendar. The
event UID is derived from `(entity_id, predicate)`, so a re-sync overwrites in
place. Non-ISO / unparseable dates are skipped. Disabled (no-op) when the
collection URL / credentials are unset.
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


async def sync_deadlines(
    db_path: str, collection_url: str, username: str, password: str
) -> dict[str, int]:
    """PUT an all-day alarmed event for every document deadline into the Solaris
    Fristen calendar. Returns `{written, skipped, failed}`. Never raises."""
    if not (collection_url and username and password):
        return {"written": 0, "skipped": 0, "failed": 0}
    client = HttpDavClient(caldav_username=username, caldav_password=password)
    written = skipped = failed = 0
    conn = projection.open_conn(db_path)
    try:
        docs = conn.execute(
            "SELECT id, canonical_name FROM entities WHERE type = 'document'"
        ).fetchall()
        events: list[tuple[str, str]] = []  # (uid, ics)
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
                events.append((uid, _build_event(uid, summary, desc, day)))
        # Dated to-do tasks (#todo) become calendar entries too: an OPEN task with
        # an ISO `due`. A resolved task simply stops being written (its event
        # lingers until the calendar is re-synced away — refined in a later slice).
        tasks = conn.execute(
            "SELECT id, canonical_name FROM entities WHERE type = 'task'"
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
            events.append((uid, _build_event(uid, summary, "", day)))
    finally:
        conn.close()
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
        except Exception as e:  # noqa: BLE001 — one bad event must not stop the sync.
            log.error("engine.deadlines_sync.event_failed", uid=uid, error=str(e))
            failed += 1
    log.info("engine.deadlines_sync", written=written, skipped=skipped, failed=failed)
    return {"written": written, "skipped": skipped, "failed": failed}

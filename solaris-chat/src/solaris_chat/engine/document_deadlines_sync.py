"""Project document deadlines into a calendar (#doc-graph, plan slice 3).

A document carries dated facts — a `cancellation_deadline`, a `hu_date`, a
contract `end_date`. Rather than push notifications, we write each as an all-day
event with an advance alarm into the owner's Radicale calendar, where the
calendar app itself reminds them (the passive, non-intrusive channel the plan
chose). Events land in a dedicated "Solaris Fristen" calendar so they're a
toggleable layer over the personal calendar.

Filesystem write like the contact sync + the Google-import (Radicale is
`owner_only`). The event UID is derived from `(entity_id, predicate)`, so a
re-sync overwrites in place instead of duplicating. Non-ISO / unparseable dates
are skipped. Disabled (no-op) when `radicale_data` is unset.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

from icalendar import Alarm, Calendar, Event

from solaris_chat import notes_search
from solaris_chat.engine.importers.google_takeout import radicale_store
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
_COLLECTION = "solaris-fristen"
_UID_PREFIX = "solaris-deadline-"
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


def sync_deadlines(db_path: str, radicale_data: str) -> dict[str, int]:
    """Write an all-day alarmed event for every document deadline into the
    owner's "Solaris Fristen" calendar. Returns `{written, skipped}`. Never
    raises — a sync failure must not abort the night run."""
    if not radicale_data:
        return {"written": 0, "skipped": 0}
    data = Path(radicale_data)
    written = skipped = 0
    conn = projection.open_conn(db_path)
    try:
        docs = conn.execute(
            "SELECT id, canonical_name, resident_uid FROM entities"
            " WHERE type = 'document' ORDER BY resident_uid"
        ).fetchall()
        with radicale_store.storage_lock(data):
            for doc in docs:
                user = doc["resident_uid"]
                if not user or user == notes_search.SHARED_UID:
                    continue
                facts = _facts(conn, doc["id"])
                provider = facts.get("provider", "")
                policy = facts.get("policy_number", "")
                coll = None
                for pred, label in _DEADLINE_LABELS.items():
                    day = _parse_iso(facts.get(pred, ""))
                    if day is None:
                        if facts.get(pred):
                            skipped += 1
                        continue
                    if coll is None:
                        coll = radicale_store.ensure_collection(
                            data,
                            user,
                            _COLLECTION,
                            tag="VCALENDAR",
                            displayname="Solaris Fristen",
                        )
                    uid = f"{_UID_PREFIX}{doc['id']}-{pred}"
                    summary = f"{label}: {doc['canonical_name']}"
                    desc = " · ".join(x for x in (provider, policy) if x)
                    ics = _build_event(uid, summary, desc, day)
                    radicale_store.write_item(coll, uid, ics, "ics")
                    written += 1
    except Exception as e:  # noqa: BLE001 — sync must never crash the night run.
        log.error("engine.deadlines_sync.failed", error=str(e))
    finally:
        conn.close()
    log.info("engine.deadlines_sync", written=written, skipped=skipped)
    return {"written": written, "skipped": skipped}

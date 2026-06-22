"""Calendar + contacts ingest adapter (#207, docs/okf-write-contract.md §6).

Reads the household CalDAV calendar + CardDAV address book **read-only** and
writes OKF concepts via the shared #447 writer:

  - **CalDAV VEVENT → event** concept: `when` from DTSTART/DTEND, `title`,
    `where` from the location, and a `with → [[people/…]]` relationship edge per
    attendee — resolved by the writer against contact person entities (so
    contacts ingested first link the attendees to real people);
  - **CardDAV vCard → person** concept: `title` = the contact name, `aliases[]`,
    `contact` = the CardDAV resource URI, and phone/email projected as `facts`
    (`phone`/`email` predicates) for later resolution.

Scope (§6): every concept is written under the configured ingesting resident
(the writer default); cross-resident sharing is an Immich-derived fact, not a
calendar/contacts default.

Idempotent + incremental: every write goes through the writer's `ingest_log`
(`source="caldav"`/`"carddav"`, the uid external_id) + `content_hash`, so a
re-run with an unchanged etag is a no-op. `iter_events`/`iter_contacts` accept a
CalDAV/CardDAV sync token so re-runs only pull changed items.
"""

from __future__ import annotations

from dataclasses import dataclass

from ...logging import log
from ..knowledge import ConceptRecord, Relationship, safe_slug
from ..knowledge.writer import OkfWriter
from .dav_client import CalEvent, Contact, DavClient


_CALDAV_SOURCE = "caldav"
_CARDDAV_SOURCE = "carddav"


@dataclass
class DavIngestStats:
    events: int = 0
    events_written: int = 0
    contacts: int = 0
    people_written: int = 0
    skipped: int = 0


class DavIngest:
    def __init__(
        self,
        client: DavClient,
        writer: OkfWriter,
        *,
        ingesting_uid: str,
    ):
        self._client = client
        self._writer = writer
        self._uid = ingesting_uid

    async def run(
        self, *, event_sync_token: str = "", contact_sync_token: str = ""
    ) -> DavIngestStats:
        """Ingest contacts then events; return run stats.

        Contacts are ingested first so an event's `with` edge resolves to an
        already-written person entity. The caller persists the sync tokens
        per collection for the next incremental run.
        """
        stats = DavIngestStats()
        async for contact in self._client.iter_contacts(sync_token=contact_sync_token):
            try:
                self._ingest_contact(contact, stats)
            except Exception as e:  # noqa: BLE001
                # One bad contact (e.g. a non-Latin name -> safe_slug ValueError)
                # must never abort the whole run (#528).
                log.error(
                    "engine.ingest.carddav_contact_failed",
                    uid=contact.uid,
                    error=str(e),
                )
                stats.skipped += 1
            stats.contacts += 1
        async for event in self._client.iter_events(sync_token=event_sync_token):
            try:
                self._ingest_event(event, stats)
            except Exception as e:  # noqa: BLE001
                log.error(
                    "engine.ingest.caldav_event_failed",
                    uid=event.uid,
                    error=str(e),
                )
                stats.skipped += 1
            stats.events += 1
        return stats

    def _ingest_contact(self, contact: Contact, stats: DavIngestStats) -> None:
        rels = [Relationship("phone", p) for p in contact.phones]
        rels += [Relationship("email", e) for e in contact.emails]
        rec = ConceptRecord(
            type="person",
            title=contact.name,
            source=_CARDDAV_SOURCE,
            external_id=contact.uid,
            resource=contact.resource,
            aliases=contact.aliases,
            # The CardDAV resource URI is the canonical `contact` link (§3).
            extra={"contact": contact.resource} if contact.resource else {},
            # Phone/email become `facts` predicates via the writer's entity
            # relationship→facts projection (later resolution).
            relationships=rels,
        )
        if not self._writer.write_concept(rec, ingesting_uid=self._uid).skipped:
            stats.people_written += 1

    def _ingest_event(self, event: CalEvent, stats: DavIngestStats) -> None:
        rels = [
            Relationship("with", f"people/{safe_slug(name)}")
            for name in event.participants
        ]
        extra: dict[str, object] = {"when": self._when(event)}
        if event.location:
            extra["where"] = event.location
        if event.participants:
            extra["participants"] = [
                f"people/{safe_slug(name)}" for name in event.participants
            ]
        rec = ConceptRecord(
            type="event",
            title=event.title,
            source=_CALDAV_SOURCE,
            external_id=event.uid,
            resident="",  # writer default = ingesting resident.
            resource=event.resource,
            description=event.description,
            timestamp=event.start,
            event_ts=event.start,
            event_kind="calendar",
            # The server etag rides the body so a changed event (new attendees,
            # moved time) moves the content_hash and re-ingests.
            body=f"CalDAV event {event.uid} (etag {event.etag})." if event.etag else "",
            extra=extra,
            relationships=rels,
        )
        if self._writer.write_concept(rec, ingesting_uid=self._uid).skipped:
            stats.skipped += 1
        else:
            stats.events_written += 1

    def _when(self, event: CalEvent) -> str:
        if event.end:
            return f"{event.start}/{event.end}"
        return event.start

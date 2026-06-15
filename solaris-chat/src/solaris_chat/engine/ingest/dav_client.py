"""Read-only CalDAV/CardDAV source client for the calendar+contacts adapter.

The adapter depends on the `DavClient` Protocol, not the concrete dav client —
so tests inject a fake and the live path uses `HttpDavClient` (a thin read-only
wrapper over a CalDAV/CardDAV server, `CALDAV_URL`/`CARDDAV_URL` +
`*_USERNAME`/`*_PASSWORD`). Read-only on the source: only `PROPFIND`/`REPORT`
collection reads, never a `PUT`/`DELETE`.

The dataclasses are the normalized subset the adapter maps from; the concrete
client folds the raw iCalendar/vCard payloads into them so the adapter never
touches DAV protocol quirks. A fake client returns the same dataclasses
directly.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class CalEvent:
    """The normalized subset of a CalDAV VEVENT the adapter ingests.

    `participants` are the attendee display names (resolved against contact
    person entities by the writer); `etag` rides the change key so an unchanged
    re-ingest is skipped.
    """

    uid: str
    title: str
    start: str  # ISO-8601 start (DTSTART).
    end: str = ""  # ISO-8601 end (DTEND), empty for an all-day/open event.
    description: str = ""
    location: str = ""
    participants: list[str] = field(default_factory=list)
    resource: str = ""  # canonical CalDAV resource URI.
    etag: str = ""  # server etag — the change key for content_hash.


@dataclass(frozen=True)
class Contact:
    """The normalized subset of a CardDAV vCard the adapter ingests.

    `phones`/`emails` project to `facts` (predicate `phone`/`email`) for later
    resolution; `etag` is the change key.
    """

    uid: str
    name: str
    aliases: list[str] = field(default_factory=list)
    phones: list[str] = field(default_factory=list)
    emails: list[str] = field(default_factory=list)
    resource: str = ""  # canonical CardDAV resource URI.
    etag: str = ""  # server etag — the change key for content_hash.


class DavClient(Protocol):
    """Read-only CalDAV/CardDAV access the adapter needs. Injectable for tests.

    Either half may be inert (its source URL unset); an inert iterator just
    yields nothing, so an operator can enable only calendar or only contacts.
    """

    def iter_events(self, *, sync_token: str = "") -> AsyncIterator[CalEvent]:
        """Yield calendar events, optionally only those changed since
        `sync_token` (the incremental CalDAV sync-collection token)."""
        ...

    def iter_contacts(self, *, sync_token: str = "") -> AsyncIterator[Contact]:
        """Yield contacts, optionally only those changed since `sync_token`
        (the incremental CardDAV sync-collection token)."""
        ...

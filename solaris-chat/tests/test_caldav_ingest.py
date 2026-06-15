"""Calendar + contacts ingest adapter (#207, docs/okf-write-contract.md §6).

The dav client is mocked (no live CalDAV/CardDAV server): a `FakeDavClient`
yields `CalEvent`/`Contact` dataclasses, so these cover the event→event and
contact→person mapping, the attendee→person `with` resolution, phone/email
facts, the idempotent skip and the incremental sync-token passthrough — without
touching the DAV protocol layer.

Schema is built from inlined DDL mirroring the #446 migration (importing alembic
from a solaris-chat test fails CI's clean env).
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator

import pytest

from solaris_chat.engine.ingest import DavIngest
from solaris_chat.engine.ingest.dav_client import CalEvent, Contact
from solaris_chat.engine.knowledge import projection
from solaris_chat.engine.knowledge.writer import OkfWriter


# Mirrors database/migrations/versions/20260615_0016_okf_knowledge_index.py.
_SCHEMA = """
CREATE TABLE entities (
  id TEXT PRIMARY KEY, type TEXT NOT NULL, canonical_name TEXT NOT NULL,
  resident_uid TEXT NOT NULL, source TEXT NOT NULL, content_hash TEXT NOT NULL,
  updated TEXT NOT NULL DEFAULT (datetime('now')));
CREATE TABLE entity_aliases (
  entity_id TEXT NOT NULL, alias TEXT NOT NULL,
  PRIMARY KEY (entity_id, alias),
  FOREIGN KEY (entity_id) REFERENCES entities (id));
CREATE INDEX entity_aliases_alias_idx ON entity_aliases (alias);
CREATE TABLE facts (
  id TEXT PRIMARY KEY, subject_entity_id TEXT, resident_uid TEXT NOT NULL,
  predicate TEXT NOT NULL, value TEXT NOT NULL, confidence REAL,
  source TEXT NOT NULL, timestamp TEXT NOT NULL DEFAULT (datetime('now')),
  FOREIGN KEY (subject_entity_id) REFERENCES entities (id));
CREATE INDEX facts_subject_predicate_idx ON facts (subject_entity_id, predicate);
CREATE TABLE events (
  id TEXT PRIMARY KEY, ts TEXT NOT NULL, resident_uid TEXT NOT NULL,
  kind TEXT NOT NULL, source TEXT NOT NULL);
CREATE INDEX events_ts_idx ON events (ts);
CREATE INDEX events_resident_ts_idx ON events (resident_uid, ts);
CREATE TABLE event_entities (
  event_id TEXT NOT NULL, entity_id TEXT NOT NULL, role TEXT NOT NULL,
  PRIMARY KEY (event_id, entity_id, role),
  FOREIGN KEY (event_id) REFERENCES events (id),
  FOREIGN KEY (entity_id) REFERENCES entities (id));
CREATE TABLE concepts (
  id TEXT PRIMARY KEY, ref_id TEXT NOT NULL,
  ref_kind TEXT NOT NULL CHECK (ref_kind IN ('entity', 'event')),
  okf_path TEXT NOT NULL, embedding_id TEXT, content_hash TEXT NOT NULL,
  updated TEXT NOT NULL DEFAULT (datetime('now')));
CREATE TABLE ingest_log (
  source TEXT NOT NULL, external_id TEXT NOT NULL, content_hash TEXT NOT NULL,
  ingested_at TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (source, external_id));
CREATE INDEX ingest_log_source_external_idx ON ingest_log (source, external_id);
"""


class FakeDavClient:
    """A mocked read-only CalDAV/CardDAV source yielding canned dataclasses."""

    def __init__(
        self,
        *,
        events: list[CalEvent] | None = None,
        contacts: list[Contact] | None = None,
    ):
        self.events = events or []
        self.contacts = contacts or []
        self.event_tokens_seen: list[str] = []
        self.contact_tokens_seen: list[str] = []

    async def iter_events(self, *, sync_token: str = "") -> AsyncIterator[CalEvent]:
        self.event_tokens_seen.append(sync_token)
        for event in self.events:
            yield event

    async def iter_contacts(self, *, sync_token: str = "") -> AsyncIterator[Contact]:
        self.contact_tokens_seen.append(sync_token)
        for contact in self.contacts:
            yield contact


@pytest.fixture
def env(tmp_path):
    db_path = str(tmp_path / "solaris.db")
    notes_dir = str(tmp_path / "notes")
    conn = sqlite3.connect(db_path)
    conn.executescript(_SCHEMA)
    conn.commit()
    conn.close()
    writer = OkfWriter(db_path=db_path, notes_dir=notes_dir)
    return writer, db_path, tmp_path


def _event(**kw) -> CalEvent:
    base = dict(
        uid="ev-1",
        title="Team Dinner",
        start="2026-05-30T19:00:00",
    )
    base.update(kw)
    return CalEvent(**base)


def _contact(**kw) -> Contact:
    base = dict(uid="c-1", name="Anna Müller")
    base.update(kw)
    return Contact(**base)


def _run(client, writer, *, uid="mdopp", event_token="", contact_token=""):
    ingest = DavIngest(client, writer, ingesting_uid=uid)
    return asyncio.run(
        ingest.run(event_sync_token=event_token, contact_sync_token=contact_token)
    )


# --- contact -> person -------------------------------------------------------


def test_contact_maps_to_person_with_aliases_and_contact_uri(env):
    writer, db_path, tmp_path = env
    client = FakeDavClient(
        contacts=[
            _contact(
                aliases=["Anna", "Anni"],
                resource="carddav://book/c-1.vcf",
            )
        ]
    )
    stats = _run(client, writer)
    assert stats.contacts == 1 and stats.people_written == 1
    conn = projection.open_conn(db_path)
    person = conn.execute("SELECT * FROM entities WHERE type = 'person'").fetchone()
    assert person["canonical_name"] == "Anna Müller"
    aliases = {
        r["alias"]
        for r in conn.execute(
            "SELECT alias FROM entity_aliases WHERE entity_id = ?", (person["id"],)
        )
    }
    assert {"Anna", "Anni"} <= aliases
    conn.close()
    text = (tmp_path / "notes" / "okf" / "people" / "anna-mueller.md").read_text()
    assert "contact: carddav://book/c-1.vcf" in text


def test_contact_phone_and_email_become_facts(env):
    writer, db_path, _ = env
    client = FakeDavClient(
        contacts=[_contact(phones=["+49 89 123"], emails=["anna@example.org"])]
    )
    _run(client, writer)
    conn = projection.open_conn(db_path)
    facts = {
        (r["predicate"], r["value"])
        for r in conn.execute("SELECT predicate, value FROM facts")
    }
    assert ("phone", "+49 89 123") in facts
    assert ("email", "anna@example.org") in facts
    conn.close()


# --- event -> event + participant resolution ---------------------------------


def test_event_maps_to_event_with_when_and_kind(env):
    writer, db_path, tmp_path = env
    client = FakeDavClient(
        events=[_event(end="2026-05-30T21:00:00", location="Club X")]
    )
    stats = _run(client, writer)
    assert stats.events == 1 and stats.events_written == 1
    conn = projection.open_conn(db_path)
    event = conn.execute("SELECT * FROM events").fetchone()
    assert event["kind"] == "calendar" and event["ts"] == "2026-05-30T19:00:00"
    concept = conn.execute(
        "SELECT okf_path FROM concepts WHERE ref_kind = 'event'"
    ).fetchone()
    assert concept["okf_path"] == "okf/events/2026-05-30-team-dinner.md"
    conn.close()
    text = (
        tmp_path / "notes" / "okf" / "events" / "2026-05-30-team-dinner.md"
    ).read_text()
    assert "when: 2026-05-30T19:00:00/2026-05-30T21:00:00" in text
    assert "where: Club X" in text


def test_event_participant_resolves_to_ingested_contact(env):
    writer, db_path, _ = env
    # Contact ingested first; the event's attendee name must resolve to that
    # person entity via the `with -> [[people/anna-mueller]]` edge.
    client = FakeDavClient(
        contacts=[_contact(name="Anna Müller")],
        events=[_event(participants=["Anna Müller"])],
    )
    _run(client, writer)
    conn = projection.open_conn(db_path)
    person = conn.execute("SELECT id FROM entities WHERE type = 'person'").fetchone()
    edge = conn.execute("SELECT * FROM event_entities WHERE role = 'with'").fetchone()
    assert edge is not None and edge["entity_id"] == person["id"]
    conn.close()


def test_event_without_matching_contact_writes_no_dangling_edge(env):
    writer, db_path, _ = env
    # No contact for the attendee -> no person entity -> no event_entities edge
    # (the writer drops an unresolvable participant rather than inventing one).
    client = FakeDavClient(events=[_event(participants=["Unknown Person"])])
    stats = _run(client, writer)
    assert stats.events_written == 1
    conn = projection.open_conn(db_path)
    assert projection.row_count(conn, "event_entities") == 0
    conn.close()


# --- incremental / idempotent ------------------------------------------------


def test_reingest_unchanged_is_skipped(env):
    writer, db_path, _ = env
    _run(FakeDavClient(events=[_event(etag="e1")]), writer)
    stats = _run(FakeDavClient(events=[_event(etag="e1")]), writer)
    assert stats.events == 1 and stats.skipped == 1 and stats.events_written == 0
    conn = projection.open_conn(db_path)
    assert projection.row_count(conn, "events") == 1
    conn.close()


def test_changed_etag_reingests_no_dup(env):
    writer, db_path, _ = env
    _run(FakeDavClient(events=[_event(etag="e1")]), writer)
    stats = _run(FakeDavClient(events=[_event(etag="e2")]), writer)
    # New etag rides the body -> content_hash moves -> not skipped, no dup.
    assert stats.events_written == 1 and stats.skipped == 0
    conn = projection.open_conn(db_path)
    assert projection.row_count(conn, "events") == 1
    conn.close()


def test_sync_tokens_are_passed_through(env):
    writer, _, _ = env
    client = FakeDavClient()
    _run(client, writer, event_token="ev-tok", contact_token="card-tok")
    assert client.event_tokens_seen == ["ev-tok"]
    assert client.contact_tokens_seen == ["card-tok"]


def test_empty_collections_are_a_noop(env):
    writer, db_path, _ = env
    stats = _run(FakeDavClient(), writer)
    assert stats == type(stats)()  # all-zero stats
    conn = projection.open_conn(db_path)
    assert projection.row_count(conn, "events") == 0
    assert projection.row_count(conn, "entities") == 0
    conn.close()

"""Immich ingest adapter (#206, docs/okf-write-contract.md §6).

The Immich client is mocked (no live instance): a `FakeImmichClient` yields
`ImmichAsset` dataclasses, so these cover the asset→event/person/place mapping,
the face→person `depicted` edge, the shared-asset→household scope, and the
incremental/idempotent skip — without touching the REST layer.

Schema is built from inlined DDL mirroring the #446 migration (importing alembic
from a solaris-chat test fails CI's clean env).
"""

from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import AsyncIterator

import aiohttp
import pytest

from solaris_chat.engine.ingest import ImmichIngest
from solaris_chat.engine.ingest.immich_client import ImmichAsset, ImmichPerson
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


class FakeImmichClient:
    """A mocked read-only Immich source yielding canned `ImmichAsset`s."""

    def __init__(self, assets: list[ImmichAsset]):
        self.assets = assets
        self.updated_after_seen: list[str] = []

    async def iter_assets(
        self, *, updated_after: str = ""
    ) -> AsyncIterator[ImmichAsset]:
        self.updated_after_seen.append(updated_after)
        for asset in self.assets:
            yield asset

    def asset_uri(self, asset_id: str) -> str:
        return f"immich://asset/{asset_id}"


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


def _asset(**kw) -> ImmichAsset:
    base = dict(
        id="a1",
        file_name="IMG_0001.jpg",
        when="2026-05-30T10:00:00",
        checksum="sha-1",
    )
    base.update(kw)
    return ImmichAsset(**base)


def _run(client, writer, *, uid="mdopp", updated_after=""):
    ingest = ImmichIngest(client, writer, ingesting_uid=uid)
    return asyncio.run(ingest.run(updated_after=updated_after))


# --- asset -> event / person / place mapping ---------------------------------


def test_asset_maps_to_event_with_media_and_resource(env):
    writer, db_path, _ = env
    client = FakeImmichClient([_asset()])
    stats = _run(client, writer)
    assert stats.assets == 1 and stats.events_written == 1
    conn = projection.open_conn(db_path)
    event = conn.execute("SELECT * FROM events").fetchone()
    assert event["kind"] == "photo" and event["ts"] == "2026-05-30T10:00:00"
    concept = conn.execute(
        "SELECT okf_path FROM concepts WHERE ref_kind = 'event'"
    ).fetchone()
    # Private asset (not shared) -> mdopp-owned, routed under the user path (#576),
    # year-sharded under okf/events/<year>/ (#830b).
    assert (
        concept["okf_path"]
        == "users/mdopp/okf/events/2026/2026-05-30-img-0001-jpg-a1.md"
    )
    conn.close()
    text = next((env[2] / "notes").glob("users/mdopp/okf/events/2026/*.md")).read_text()
    assert "resource: immich://asset/a1" in text
    assert "media:" in text and "- immich://asset/a1" in text


def test_named_face_creates_person_and_depicted_edge(env):
    writer, db_path, tmp_path = env
    client = FakeImmichClient(
        [_asset(people=[ImmichPerson(id="p1", name="Anna Müller")])]
    )
    stats = _run(client, writer)
    assert stats.people_written == 1
    conn = projection.open_conn(db_path)
    person = conn.execute("SELECT * FROM entities WHERE type = 'person'").fetchone()
    assert person["canonical_name"] == "Anna Müller"
    edge = conn.execute("SELECT * FROM event_entities").fetchone()
    assert edge["role"] == "depicted" and edge["entity_id"] == person["id"]
    conn.close()
    person_path = (
        tmp_path / "notes" / "users" / "mdopp" / "okf" / "people" / "anna-mueller.md"
    )
    assert person_path.is_file()


def test_unnamed_face_is_not_ingested(env):
    writer, db_path, _ = env
    # An Immich face cluster with no name yields no ImmichPerson (filtered in
    # the client); the adapter must not write an entity or a depicted edge.
    client = FakeImmichClient([_asset(people=[])])
    _run(client, writer)
    conn = projection.open_conn(db_path)
    assert projection.row_count(conn, "entities") == 0
    assert projection.row_count(conn, "event_entities") == 0
    conn.close()


def test_geo_creates_place_and_event_at_edge(env):
    writer, db_path, tmp_path = env
    client = FakeImmichClient(
        [_asset(latitude=48.1372, longitude=11.5756, city="München", country="DE")]
    )
    stats = _run(client, writer)
    assert stats.places_written == 1
    conn = projection.open_conn(db_path)
    place = conn.execute("SELECT * FROM entities WHERE type = 'place'").fetchone()
    assert place["canonical_name"] == "München, DE"
    edge = conn.execute("SELECT * FROM event_entities WHERE role = 'at'").fetchone()
    assert edge["entity_id"] == place["id"]
    conn.close()
    place_text = (
        tmp_path / "notes" / "users" / "mdopp" / "okf" / "places" / "muenchen-de.md"
    ).read_text()
    assert "geo: 48.1372,11.5756" in place_text


def test_asset_without_geo_writes_no_place(env):
    writer, db_path, _ = env
    _run(FakeImmichClient([_asset()]), writer)
    conn = projection.open_conn(db_path)
    assert projection.row_count(conn, "entities") == 0  # no person, no place
    conn.close()


# --- shared scope ------------------------------------------------------------


def test_shared_asset_maps_to_household_scope(env):
    writer, db_path, _ = env
    client = FakeImmichClient(
        [
            _asset(
                people=[ImmichPerson(id="p1", name="Anna")],
                latitude=48.0,
                longitude=11.0,
                city="Munich",
                shared_with=["lena", "mdopp"],
            )
        ]
    )
    _run(client, writer, uid="mdopp")
    conn = projection.open_conn(db_path)
    # Shared → every concept (event/person/place) is household-scoped (§6).
    scopes = {r[0] for r in conn.execute("SELECT DISTINCT resident_uid FROM events")}
    scopes |= {r[0] for r in conn.execute("SELECT DISTINCT resident_uid FROM entities")}
    assert scopes == {"household"}
    conn.close()


def test_unshared_asset_defaults_to_ingesting_resident(env):
    writer, db_path, _ = env
    client = FakeImmichClient([_asset(people=[ImmichPerson(id="p1", name="Anna")])])
    _run(client, writer, uid="lena")
    conn = projection.open_conn(db_path)
    assert conn.execute("SELECT resident_uid FROM events").fetchone()[0] == "lena"
    assert conn.execute("SELECT resident_uid FROM entities").fetchone()[0] == "lena"
    conn.close()


# --- incremental / idempotent ------------------------------------------------


def test_reingest_unchanged_is_skipped(env):
    writer, db_path, _ = env
    client = FakeImmichClient([_asset()])
    _run(client, writer)
    stats = _run(FakeImmichClient([_asset()]), writer)
    assert stats.assets == 1 and stats.skipped == 1 and stats.events_written == 0
    conn = projection.open_conn(db_path)
    assert projection.row_count(conn, "events") == 1
    assert projection.row_count(conn, "ingest_log") == 1
    conn.close()


def test_changed_checksum_reingests(env):
    writer, db_path, _ = env
    _run(FakeImmichClient([_asset(checksum="sha-1")]), writer)
    stats = _run(FakeImmichClient([_asset(checksum="sha-2")]), writer)
    # New checksum rides the body -> content_hash moves -> not skipped, no dup.
    assert stats.events_written == 1 and stats.skipped == 0
    conn = projection.open_conn(db_path)
    assert projection.row_count(conn, "events") == 1
    log = conn.execute(
        "SELECT content_hash FROM ingest_log WHERE external_id = 'asset/a1'"
    ).fetchone()
    assert log is not None
    conn.close()


def test_updated_after_cursor_is_passed_through(env):
    writer, _, _ = env
    client = FakeImmichClient([])
    _run(client, writer, updated_after="2026-05-01T00:00:00")
    assert client.updated_after_seen == ["2026-05-01T00:00:00"]


def test_bad_asset_is_skipped_and_the_rest_still_ingest(env):
    # An asset whose only depicted person has a purely non-Latin name ->
    # safe_slug ValueError. Without per-item isolation it would abort the whole
    # run (#528); it must be skipped and the next asset must still ingest.
    writer, db_path, _ = env
    bad = _asset(id="bad", checksum="cb", people=[ImmichPerson(id="p1", name="王芳")])
    ok = _asset(id="ok", checksum="co")
    stats = _run(FakeImmichClient([bad, ok]), writer)
    assert stats.assets == 2 and stats.skipped == 1 and stats.events_written == 1
    conn = projection.open_conn(db_path)
    assert projection.row_count(conn, "events") == 1
    conn.close()


def test_run_returns_high_water_cursor(env):
    # The run reports the max asset `when` so the caller can persist it as the
    # next incremental cursor (#529).
    writer, _, _ = env
    a1 = _asset(id="a1", checksum="c1", when="2026-05-01T00:00:00")
    a2 = _asset(id="a2", checksum="c2", when="2026-05-30T10:00:00")
    stats = _run(FakeImmichClient([a1, a2]), writer)
    assert stats.cursor == "2026-05-30T10:00:00"


# --- transport: per-page retry on a keep-alive drop (#597) -------------------


class _RetryResp:
    def __init__(self, payload):
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def raise_for_status(self):
        return None

    async def json(self):
        return self._payload


class _RetrySession:
    """A fake aiohttp session whose POST raises ServerDisconnectedError on the
    first `fail` attempts of every page, then returns a one-page payload."""

    def __init__(self, *, fail: int):
        self._fail = fail
        self.posts = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def post(self, url, *, json, headers):
        self.posts += 1
        if self.posts <= self._fail:
            import aiohttp as _a

            raise _a.ServerDisconnectedError("keep-alive drop")
        # A single page with no nextPage -> the iterator stops after it.
        return _RetryResp({"assets": {"items": [{"id": "a1"}], "nextPage": None}})


def _drain(client):
    async def go():
        return [a async for a in client.iter_assets()]

    return asyncio.run(go())


def test_iter_assets_retries_a_disconnected_page(monkeypatch):
    from solaris_chat.engine.ingest import immich_client as mod

    session = _RetrySession(fail=1)  # first attempt drops, retry recovers.
    monkeypatch.setattr(mod.aiohttp, "ClientSession", lambda *a, **k: session)

    async def _no_sleep(*a, **k):
        return None

    monkeypatch.setattr(mod.asyncio, "sleep", _no_sleep)
    client = mod.RestImmichClient("http://immich", "k")
    assets = _drain(client)
    # The page was still yielded (no abort) and the POST was retried once.
    assert [a.id for a in assets] == ["a1"]
    assert session.posts == 2


def test_iter_assets_raises_after_retries_exhausted(monkeypatch):
    from solaris_chat.engine.ingest import immich_client as mod

    session = _RetrySession(fail=999)  # never recovers.
    monkeypatch.setattr(mod.aiohttp, "ClientSession", lambda *a, **k: session)

    async def _no_sleep(*a, **k):
        return None

    monkeypatch.setattr(mod.asyncio, "sleep", _no_sleep)
    client = mod.RestImmichClient("http://immich", "k")
    with pytest.raises(aiohttp.ClientError):
        _drain(client)
    # First try + the bounded retries, then it gives up.
    assert session.posts == len(mod._PAGE_RETRY_BACKOFF) + 1


# --- cursor checkpoint: partial progress persists mid-run (#597) -------------


def test_run_checkpoints_cursor_on_partial_progress():
    # A client that yields several assets then raises mid-stream; the run must
    # have invoked the checkpoint with a cursor > the start, so the next boot
    # resumes from there instead of re-paging from page 1.
    saved: list[str] = []

    class _AbortingClient:
        def asset_uri(self, asset_id):
            return f"immich://{asset_id}"

        async def iter_assets(self, *, updated_after=""):
            for i in range(1, ImmichIngest._CHECKPOINT_EVERY + 1):
                yield _asset(
                    id=f"a{i}",
                    checksum=f"c{i}",
                    when=f"2026-05-{(i % 28) + 1:02d}T00:00:00",
                )
            raise aiohttp.ServerDisconnectedError("dropped mid-run")

    import sqlite3
    import tempfile

    tmp = tempfile.mkdtemp()
    db_path = f"{tmp}/solaris.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(_SCHEMA)
    conn.commit()
    conn.close()
    writer = OkfWriter(db_path=db_path, notes_dir=f"{tmp}/notes")
    ingest = ImmichIngest(_AbortingClient(), writer, ingesting_uid="mdopp")

    async def go():
        await ingest.run(updated_after="", checkpoint=saved.append)

    with pytest.raises(aiohttp.ServerDisconnectedError):
        asyncio.run(go())
    # A checkpoint fired on partial progress -> cursor advanced past the start.
    assert saved and saved[-1] > ""


def test_run_does_not_checkpoint_without_progress():
    # A short run (fewer than _CHECKPOINT_EVERY assets) completes without ever
    # checkpointing mid-stream (the caller still saves the final cursor).
    saved: list[str] = []

    import sqlite3
    import tempfile

    tmp = tempfile.mkdtemp()
    db_path = f"{tmp}/solaris.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(_SCHEMA)
    conn.commit()
    conn.close()
    writer = OkfWriter(db_path=db_path, notes_dir=f"{tmp}/notes")
    client = FakeImmichClient([_asset()])
    ingest = ImmichIngest(client, writer, ingesting_uid="mdopp")
    asyncio.run(ingest.run(checkpoint=saved.append))
    assert saved == []


def test_same_place_dedups_across_assets(env):
    writer, db_path, _ = env
    a1 = _asset(id="a1", checksum="c1", latitude=48.0, longitude=11.0, city="Munich")
    a2 = _asset(id="a2", checksum="c2", latitude=48.0, longitude=11.0, city="Munich")
    _run(FakeImmichClient([a1, a2]), writer)
    conn = projection.open_conn(db_path)
    # Two assets at one spot -> two events, one shared place concept.
    assert projection.row_count(conn, "events") == 2
    assert projection.row_count(conn, "entities") == 1
    conn.close()

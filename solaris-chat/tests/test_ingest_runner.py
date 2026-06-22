"""On-boot OKF ingest trigger (#517).

The runner is the missing trigger that actually runs the Phase-1 adapters on
the box. These tests prove: (1) the Obsidian adapter runs against the local
vault and produces real OKF concept files + projection rows (the core
acceptance — okf/*.md exists, entities/ingest_log > 0); (2) Immich runs only
when configured and is mocked (no live network in CI); (3) CalDAV/CardDAV and
an unconfigured Immich degrade gracefully (log + skip, no crash); (4) one
adapter raising never crashes the trigger.

Schema is built from inlined DDL mirroring the #446 migration (importing
alembic from a solaris-chat test fails CI's clean env).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field

import pytest

from solaris_chat.engine import ingest
from solaris_chat.engine.ingest import runner


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


@dataclass
class FakeSettings:
    solaris_db_path: str
    notes_dir: str
    default_uid: str = "household"
    immich_base_url: str = ""
    immich_api_key: str = ""
    caldav_url: str = ""
    caldav_username: str = ""
    caldav_password: str = ""
    carddav_url: str = ""
    carddav_username: str = ""
    carddav_password: str = ""


@pytest.fixture
def env(tmp_path):
    db_path = str(tmp_path / "solaris.db")
    notes_dir = tmp_path / "notes"
    notes_dir.mkdir()
    conn = sqlite3.connect(db_path)
    conn.executescript(_SCHEMA)
    conn.commit()
    conn.close()
    return db_path, notes_dir


def _seed_vault(notes_dir):
    """A small hand-written vault: a person note + a diary that links it."""
    (notes_dir / "people").mkdir()
    (notes_dir / "people" / "anna.md").write_text(
        "---\ntype: person\ntitle: Anna\n---\n\n# Anna\n\nA friend.\n",
        encoding="utf-8",
    )
    (notes_dir / "diary.md").write_text(
        "---\ntitle: Diary\n---\n\nMet [[Anna]] today.\n", encoding="utf-8"
    )


def _counts(db_path):
    conn = sqlite3.connect(db_path)
    try:
        return {
            t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("entities", "ingest_log", "concepts", "facts")
        }
    finally:
        conn.close()


# --- the core acceptance: Obsidian produces real OKF concepts ----------------


async def test_run_ingest_obsidian_produces_okf_and_projection(env):
    db_path, notes_dir = env
    _seed_vault(notes_dir)
    await runner.run_ingest(
        FakeSettings(solaris_db_path=db_path, notes_dir=str(notes_dir))
    )
    # okf/*.md exists with real concepts.
    assert (notes_dir / "okf" / "people" / "anna.md").is_file()
    assert (notes_dir / "okf" / "notes" / "diary.md").is_file()
    # entities / ingest_log / concepts have > 0 rows.
    counts = _counts(db_path)
    assert counts["entities"] >= 2
    assert counts["ingest_log"] >= 2
    assert counts["concepts"] >= 2


async def test_run_ingest_enqueues_concept_embeddings(env, tmp_path):
    db_path, notes_dir = env
    _seed_vault(notes_dir)
    await runner.run_ingest(
        FakeSettings(solaris_db_path=db_path, notes_dir=str(notes_dir))
    )
    # The runner uses the durable PendingEmbeddingQueue -> a sidecar with > 0.
    queue = tmp_path / "okf_embedding_queue.json"
    assert queue.is_file()
    import json

    assert len(json.loads(queue.read_text())) >= 2


# --- Immich: configured -> runs (mocked); unconfigured -> skipped ------------


@dataclass(frozen=True)
class _FakeAsset:
    id: str
    file_name: str
    when: str
    checksum: str
    latitude: float | None = None
    longitude: float | None = None
    city: str = ""
    state: str = ""
    country: str = ""
    people: list = field(default_factory=list)
    shared_with: list = field(default_factory=list)


class _FakeImmich:
    """Stands in for RestImmichClient — no network."""

    def __init__(self, *args, **kwargs):
        pass

    async def iter_assets(self, *, updated_after: str = ""):
        yield _FakeAsset(
            id="a1", file_name="beach.jpg", when="2026-05-01", checksum="x"
        )

    def asset_uri(self, asset_id: str) -> str:
        return f"immich://{asset_id}"


async def test_run_ingest_immich_runs_when_configured(env, monkeypatch):
    db_path, notes_dir = env
    monkeypatch.setattr(runner, "RestImmichClient", _FakeImmich)
    await runner.run_ingest(
        FakeSettings(
            solaris_db_path=db_path,
            notes_dir=str(notes_dir),
            immich_base_url="http://immich",
            immich_api_key="k",
        )
    )
    # The mocked asset produced an OKF event concept + projection rows.
    assert _counts(db_path)["concepts"] >= 1
    assert list((notes_dir / "okf").rglob("*.md"))


async def test_run_ingest_immich_skipped_when_unconfigured(env, monkeypatch):
    db_path, notes_dir = env

    def _boom(*a, **k):  # the client must never be built when unconfigured.
        raise AssertionError("Immich client built without config")

    monkeypatch.setattr(runner, "RestImmichClient", _boom)
    # No creds -> Immich is skipped; no crash.
    await runner.run_ingest(
        FakeSettings(solaris_db_path=db_path, notes_dir=str(notes_dir))
    )


# --- graceful degradation -----------------------------------------------------


async def test_run_ingest_caldav_skipped_when_unconfigured(env, monkeypatch):
    db_path, notes_dir = env

    def _boom(*a, **k):  # the client must never be built when unconfigured.
        raise AssertionError("DAV client built without config")

    monkeypatch.setattr(runner, "HttpDavClient", _boom)
    # No CalDAV/CardDAV URL -> skipped; no crash, no client built.
    await runner.run_ingest(
        FakeSettings(solaris_db_path=db_path, notes_dir=str(notes_dir))
    )


class _FakeContact:
    """Stands in for a Contact dataclass from a mocked DAV client."""

    uid = "c-1"
    name = "Anna Müller"
    aliases: list = []
    phones = ["+49 89 123"]
    emails: list = []
    resource = "carddav://book/c-1.vcf"
    etag = "e1"


class _FakeDav:
    """Stands in for HttpDavClient — no network."""

    def __init__(self, *args, **kwargs):
        pass

    async def iter_contacts(self, *, sync_token: str = ""):
        yield _FakeContact()

    async def iter_events(self, *, sync_token: str = ""):
        return
        yield  # pragma: no cover — makes this an async generator.


async def test_run_ingest_caldav_runs_when_configured(env, monkeypatch):
    db_path, notes_dir = env
    monkeypatch.setattr(runner, "HttpDavClient", _FakeDav)
    await runner.run_ingest(
        FakeSettings(
            solaris_db_path=db_path,
            notes_dir=str(notes_dir),
            caldav_url="https://radicale/cal",
            carddav_url="https://radicale/contacts",
        )
    )
    # The mocked contact produced a person concept + a phone fact.
    counts = _counts(db_path)
    assert counts["entities"] >= 1
    assert counts["facts"] >= 1


async def test_run_ingest_survives_an_adapter_failure(env, monkeypatch):
    db_path, notes_dir = env
    _seed_vault(notes_dir)

    class _BadImmich:
        def __init__(self, *a, **k):
            raise RuntimeError("immich down")

    monkeypatch.setattr(runner, "RestImmichClient", _BadImmich)
    # Immich blows up, but Obsidian still ran and the trigger did not raise.
    await runner.run_ingest(
        FakeSettings(
            solaris_db_path=db_path,
            notes_dir=str(notes_dir),
            immich_base_url="http://immich",
            immich_api_key="k",
        )
    )
    assert _counts(db_path)["entities"] >= 2  # Obsidian wrote despite Immich failing


# --- the trigger is exported for the boot wiring -----------------------------


def test_run_ingest_is_exported_from_ingest_package():
    assert ingest.run_ingest is runner.run_ingest

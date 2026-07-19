"""Tests for the durable import-job runner (#864).

Covers enqueue → dispatch → complete, failure capture, progress persistence,
cancel, owner scoping, the registered-kind lookup and boot-time resume — all
against a real ``engine_import_jobs`` sqlite table. One test replays the alembic
chain to head so the 0025 migration is exercised too.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from solaris_chat.engine.importers import jobs as jobs_mod
from solaris_chat.engine.importers.jobs import JobRunner, registered_kind, runner

_DB_DIR = Path(__file__).resolve().parents[2].parent / "database"

# The 0025 migration creates this, replayed locally so the runner tests run
# against a real sqlite db without alembic.
_SCHEMA = """
CREATE TABLE engine_import_jobs (
  id         TEXT PRIMARY KEY,
  owner_uid  TEXT NOT NULL,
  kind       TEXT NOT NULL,
  status     TEXT NOT NULL DEFAULT 'pending',
  payload    TEXT NOT NULL DEFAULT '{}',
  progress   TEXT NOT NULL DEFAULT '{}',
  error      TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX engine_import_jobs_status_idx ON engine_import_jobs (status);
CREATE INDEX engine_import_jobs_owner_idx ON engine_import_jobs (owner_uid, created_at);
"""


@pytest.fixture()
def db(tmp_path) -> str:
    path = str(tmp_path / "solaris.db")
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA)
    conn.close()
    return path


@pytest.fixture(autouse=True)
def _clean_runners():
    """Each test registers its own runners; don't leak them across tests."""
    saved = dict(jobs_mod._RUNNERS)
    jobs_mod._RUNNERS.clear()
    yield
    jobs_mod._RUNNERS.clear()
    jobs_mod._RUNNERS.update(saved)


def _wait(runner_obj: JobRunner, jid: str, statuses: set[str], owner="mdopp"):
    for _ in range(200):
        snap = runner_obj.get(jid, owner)
        if snap and snap["status"] in statuses:
            return snap
        time.sleep(0.01)
    raise AssertionError(f"job {jid} never reached {statuses}: {runner_obj.get(jid)}")


# ---- migration replay ----


@pytest.mark.skipif(
    not (_DB_DIR / "alembic.ini").exists(), reason="database/ migrations not present"
)
def test_migrations_replay_to_head_create_import_jobs(tmp_path):
    """The 0025 migration replays on top of head and lands engine_import_jobs."""
    pytest.importorskip("alembic")
    from alembic.command import upgrade
    from alembic.config import Config
    from alembic.util.exc import CommandError

    dbp = tmp_path / "replay.db"
    cfg = Config(str(_DB_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(_DB_DIR / "migrations"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{dbp}")
    try:
        upgrade(cfg, "head")
    except (CommandError, KeyError) as exc:
        pytest.skip(f"migration chain incomplete: {exc}")

    conn = sqlite3.connect(str(dbp))
    cols = {r[1] for r in conn.execute("PRAGMA table_info(engine_import_jobs)")}
    idx = {r[1] for r in conn.execute("PRAGMA index_list(engine_import_jobs)")}
    conn.close()
    assert {
        "id",
        "owner_uid",
        "kind",
        "status",
        "payload",
        "progress",
        "error",
        "created_at",
        "updated_at",
    } <= cols
    assert "engine_import_jobs_status_idx" in idx


# ---- runner behaviour ----


def test_enqueue_dispatch_complete(db):
    seen = []

    @runner("echo")
    def _build(payload):
        def factory(is_canceled):
            for i in range(3):
                seen.append(i)
                yield {"pct": i * 33, "stage": "work", "done": i, "total": 3}
            yield {"pct": 100, "result": {"items": payload["n"]}}

        return factory

    r = JobRunner(db)
    jid = r.start("mdopp", "echo", {"n": 7})
    snap = _wait(r, jid, {"done"})

    assert snap["status"] == "done"
    assert snap["progress"]["pct"] == 100
    assert snap["result"] == {"items": 7}
    assert snap["error"] is None
    assert seen == [0, 1, 2]


def test_progress_persisted_between_yields(db):
    started = []

    @runner("slow")
    def _build(payload):
        def factory(is_canceled):
            yield {"pct": 10, "stage": "phase-1", "message": "go"}
            started.append(True)
            while len(started) < 2:
                time.sleep(0.005)
            yield {"pct": 100}

        return factory

    r = JobRunner(db)
    jid = r.start("mdopp", "slow", {})
    # First yield lands before we release the second.
    for _ in range(200):
        snap = r.get(jid, "mdopp")
        if snap["progress"].get("stage") == "phase-1":
            break
        time.sleep(0.01)
    assert snap["status"] == "running"
    assert snap["progress"]["message"] == "go"
    started.append(True)  # release
    _wait(r, jid, {"done"})


def test_failure_captured(db):
    @runner("boom")
    def _build(payload):
        def factory(is_canceled):
            yield {"pct": 5}
            raise ValueError("kaboom")

        return factory

    r = JobRunner(db)
    jid = r.start("mdopp", "boom", {})
    snap = _wait(r, jid, {"failed"})

    assert snap["status"] == "failed"
    assert "ValueError" in snap["error"]
    assert "kaboom" in snap["error"]


def test_cancel_interrupts(db):
    @runner("loop")
    def _build(payload):
        def factory(is_canceled):
            for i in range(1000):
                if is_canceled():
                    return
                yield {"pct": i, "stage": "spin"}
                time.sleep(0.005)

        return factory

    r = JobRunner(db)
    jid = r.start("mdopp", "loop", {})
    for _ in range(200):
        if r.get(jid, "mdopp")["progress"].get("stage") == "spin":
            break
        time.sleep(0.01)
    assert r.cancel(jid, "mdopp") is True
    snap = _wait(r, jid, {"interrupted"})
    assert snap["status"] == "interrupted"


def test_owner_scoping(db):
    @runner("noop")
    def _build(payload):
        def factory(is_canceled):
            yield {"pct": 100}

        return factory

    r = JobRunner(db)
    jid = r.start("mdopp", "noop", {})
    _wait(r, jid, {"done"})

    assert r.get(jid, "lena") is None  # not her job
    assert r.get(jid, "mdopp") is not None
    assert r.cancel(jid, "lena") is False
    assert r.latest_for("lena") is None
    latest = r.latest_for("mdopp")
    assert latest["jobId"] == jid


def test_registered_kind_lookup(db):
    @runner("wired")
    def _build(payload):
        def factory(is_canceled):
            yield {}

        return factory

    assert registered_kind("wired") is True
    # google_takeout is a known importer registry key (vendored in #863).
    assert registered_kind("google_takeout") is True
    assert registered_kind("nonexistent") is False


def test_resume_respawns_running(db):
    ran = []

    @runner("res")
    def _build(payload):
        def factory(is_canceled):
            ran.append(payload["tag"])
            yield {"pct": 100}

        return factory

    # A row left `running` (as if the process died mid-job), plus one with an
    # unknown kind that must be marked interrupted.
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO engine_import_jobs (id, owner_uid, kind, status, payload)"
        " VALUES ('j1', 'mdopp', 'res', 'running', '{\"tag\": \"a\"}')"
    )
    conn.execute(
        "INSERT INTO engine_import_jobs (id, owner_uid, kind, status, payload)"
        " VALUES ('j2', 'mdopp', 'ghost', 'running', '{}')"
    )
    conn.commit()
    conn.close()

    r = JobRunner(db)
    r.resume("mdopp")
    snap = _wait(r, "j1", {"done"})
    assert snap["status"] == "done"
    assert ran == ["a"]
    assert r.get("j2", "mdopp")["status"] == "interrupted"


def test_resume_owner_scoped(db):
    @runner("res")
    def _build(payload):
        def factory(is_canceled):
            yield {"pct": 100}

        return factory

    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO engine_import_jobs (id, owner_uid, kind, status, payload)"
        " VALUES ('mine', 'mdopp', 'res', 'running', '{}')"
    )
    conn.execute(
        "INSERT INTO engine_import_jobs (id, owner_uid, kind, status, payload)"
        " VALUES ('hers', 'lena', 'res', 'running', '{}')"
    )
    conn.commit()
    conn.close()

    r = JobRunner(db)
    r.resume("mdopp")
    _wait(r, "mine", {"done"})
    # lena's row is untouched by mdopp's owner-scoped resume.
    assert r.get("hers", "lena")["status"] == "running"

"""Tests for the engine night jobs (Phase 3) — code-defined crons with
durable last-run stamps, run on the deep profile."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from solilos_chat.engine import crons

from tests.test_engine import _SCHEMA

_TZ = ZoneInfo("Europe/Berlin")

_CRON_SCHEMA = (
    _SCHEMA
    + """
CREATE TABLE engine_cron_runs (
  name     TEXT PRIMARY KEY,
  last_run TEXT NOT NULL
);
"""
)


@pytest.fixture
def db(tmp_path) -> str:
    path = str(tmp_path / "solilos.db")
    conn = sqlite3.connect(path)
    conn.executescript(_CRON_SCHEMA)
    conn.commit()
    conn.close()
    return path


class _FakeDeep:
    def __init__(self):
        self.turns = []
        self.created = []
        self.deleted = []

    async def create_session(self, uid, system_prompt=None, **kw):
        self.created.append((uid, kw))
        return f"cron-sess-{len(self.created)}"

    async def delete_session(self, session_id):
        self.deleted.append(session_id)
        return True

    async def chat(self, session_id, text, images=None, reasoning_effort="none"):
        self.turns.append((session_id, text, reasoning_effort))
        return "done"


def _runner(db, deep, skills_dir="", jobs=crons.JOBS):
    return crons.CronRunner(
        db_path=db, deep=deep, skills_dir=skills_dir, context_window=32768, jobs=jobs
    )


def _baseline(db, name, stamp="2020-01-01T00:00:00+01:00"):
    """A pre-existing (old) last-run stamp — past first-boot baselining."""
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO engine_cron_runs (name, last_run) VALUES (?, ?)", (name, stamp)
    )
    conn.commit()
    conn.close()


def test_jobs_match_hermes_era_schedules():
    by_name = {j.name: j for j in crons.JOBS}
    assert (by_name["daily-chronicle"].hour, by_name["daily-chronicle"].minute) == (
        23,
        59,
    )
    assert by_name["daily-chronicle"].weekday is None
    assert by_name["problem-summarizer"].weekday == 0  # Monday
    assert (by_name["chat-compactor"].hour, by_name["chat-compactor"].minute) == (4, 15)


def test_slot_daily_and_weekly():
    job = crons.CronJob(name="d", minute=59, hour=23)
    now = datetime(2026, 6, 12, 0, 5, tzinfo=_TZ)
    assert (
        crons._slot(job, now) == datetime(2026, 6, 11, 23, 59, tzinfo=_TZ).isoformat()
    )
    weekly = crons.CronJob(name="w", minute=30, hour=4, weekday=0)
    now = datetime(2026, 6, 12, 12, 0, tzinfo=_TZ)  # Friday
    assert (
        crons._slot(weekly, now)
        == datetime(2026, 6, 8, 4, 30, tzinfo=_TZ).isoformat()  # the past Monday
    )


async def test_due_job_fires_once_per_slot(db):
    deep = _FakeDeep()
    job = crons.CronJob(name="daily-chronicle", minute=59, hour=23, prompt="Schreibe.")
    _baseline(db, "daily-chronicle")
    runner = _runner(db, deep, jobs=(job,))
    now = datetime(2026, 6, 12, 0, 5, tzinfo=_TZ)
    await runner.tick(now)
    await runner.tick(now)  # same slot — must not double-run
    assert len(deep.turns) == 1
    sid, text, effort = deep.turns[0]
    assert text.endswith("Schreibe.")
    assert effort == "high"
    # Ephemeral cron session is cleaned up after the run.
    assert deep.created[0][1]["ephemeral"] is True
    assert deep.deleted == [sid]


async def test_restart_after_slot_fires_late_not_skipped(db):
    deep = _FakeDeep()
    job = crons.CronJob(name="daily-chronicle", minute=59, hour=23, prompt="Schreibe.")
    _baseline(db, "daily-chronicle")
    runner = _runner(db, deep, jobs=(job,))
    # The tick happens hours after the slot (e.g. the box was down at 23:59).
    now = datetime(2026, 6, 12, 7, 0, tzinfo=_TZ)
    await runner.tick(now)
    assert len(deep.turns) == 1


async def test_first_boot_baselines_without_running(db):
    # A fresh install must not back-run last night's job mid-day: the first
    # tick stamps the current slot and runs nothing; the NEXT slot fires.
    deep = _FakeDeep()
    job = crons.CronJob(name="daily-chronicle", minute=59, hour=23, prompt="Schreibe.")
    runner = _runner(db, deep, jobs=(job,))
    await runner.tick(datetime(2026, 6, 12, 12, 0, tzinfo=_TZ))
    assert deep.turns == []
    await runner.tick(datetime(2026, 6, 13, 0, 5, tzinfo=_TZ))
    assert len(deep.turns) == 1


async def test_skill_body_prepended(db, tmp_path):
    skills_dir = tmp_path / "skills"
    skill = skills_dir / "daily-chronicle"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: daily-chronicle\n---\n# Chronik\nSo geht das.",
        encoding="utf-8",
    )
    deep = _FakeDeep()
    job = crons.CronJob(
        name="daily-chronicle",
        minute=59,
        hour=23,
        prompt="Schreibe.",
        skill="daily-chronicle",
    )
    _baseline(db, "daily-chronicle")
    runner = _runner(db, deep, skills_dir=str(skills_dir), jobs=(job,))
    await runner.tick(datetime(2026, 6, 12, 0, 5, tzinfo=_TZ))
    _, text, _ = deep.turns[0]
    assert text.startswith("# Chronik")
    assert "So geht das." in text
    assert text.endswith("Schreibe.")


async def test_compactor_picks_stale_long_sessions(db, monkeypatch):
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO engine_sessions (id, owner_uid, input_tokens, output_tokens,"
        " last_activity) VALUES ('old-long', 'anna', 30000, 2000,"
        " datetime('now', '-30 days'))"
    )
    conn.execute(
        "INSERT INTO engine_sessions (id, owner_uid, input_tokens, output_tokens,"
        " last_activity) VALUES ('old-short', 'anna', 100, 10,"
        " datetime('now', '-30 days'))"
    )
    conn.execute(
        "INSERT INTO engine_sessions (id, owner_uid, input_tokens, output_tokens,"
        " last_activity) VALUES ('fresh-long', 'anna', 30000, 2000,"
        " datetime('now'))"
    )
    conn.commit()
    conn.close()

    compacted = []

    async def fake_compact(client, uid, session_id, *, context_window, force=False):
        compacted.append((session_id, force))
        return "continuation-1"

    monkeypatch.setattr(crons.compaction, "compact_session", fake_compact)
    deep = _FakeDeep()
    job = crons.CronJob(name="chat-compactor", minute=15, hour=4)
    _baseline(db, "chat-compactor")
    runner = _runner(db, deep, jobs=(job,))
    await runner.tick(datetime(2026, 6, 12, 4, 20, tzinfo=_TZ))
    assert compacted == [("old-long", True)]

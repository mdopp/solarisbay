"""Tests for the engine night jobs (Phase 3) — code-defined crons with
durable last-run stamps, run on the deep profile."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from solaris_chat.engine import crons

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
    path = str(tmp_path / "solaris.db")
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

    async def delete_session(self, session_id, uid):
        self.deleted.append((session_id, uid))
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


def _write_scheduler(skills_dir, def_id, schedule, body):
    d = skills_dir / def_id
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {def_id}\nkind: scheduler\nschedule: {schedule}\n---\n\n{body}\n",
        encoding="utf-8",
    )


def test_parse_schedule_shapes():
    assert crons._parse_schedule("59 23 * * *") == (59, 23, None)
    assert crons._parse_schedule("30 4 * * mon") == (30, 4, 0)
    assert crons._parse_schedule("30 4 * * 0") == (30, 4, 0)
    assert crons._parse_schedule("0 9 5 * *") is None  # day-of-month unsupported
    assert crons._parse_schedule("99 0 * * *") is None  # minute out of range
    assert crons._parse_schedule("not cron") is None


def test_load_jobs_from_scheduler_defs(tmp_path):
    skills_dir = tmp_path / "skills"
    _write_scheduler(
        skills_dir, "daily-chronicle", "59 23 * * *", "Schreibe die Chronik."
    )
    _write_scheduler(skills_dir, "weekly-x", "30 4 * * mon", "Mach den Wochenjob.")
    jobs = crons.load_jobs(str(skills_dir))
    by_name = {j.name: j for j in jobs}
    # The code compactor is always present alongside the scheduler defs.
    assert "chat-compactor" in by_name and by_name["chat-compactor"].prompt == ""
    chron = by_name["daily-chronicle"]
    assert (chron.hour, chron.minute, chron.weekday) == (23, 59, None)
    assert chron.prompt == "Schreibe die Chronik."
    assert by_name["weekly-x"].weekday == 0


def test_load_jobs_from_shipped_pack_builds_the_three_jobs():
    # The #484 reorg gives the three cron defs scheduler-kind frontmatter, so the
    # registry — not the hardcoded JOBS fallback — must build them. The compactor
    # stays the code job (empty prompt) even though it is now a scheduler def.
    pack = (
        Path(__file__).resolve().parents[2]
        / "templates"
        / "solaris"
        / "skills"
        / "household"
    )
    jobs = crons.load_jobs(str(pack))
    assert jobs != crons.JOBS  # built from the registry, not the fallback
    by_name = {j.name: j for j in jobs}
    assert set(by_name) == {
        "chat-compactor",
        "knowledge-night-run",
        "daily-chronicle",
        "problem-summarizer",
    }
    compactor = by_name["chat-compactor"]
    assert (compactor.hour, compactor.minute, compactor.prompt) == (4, 15, "")
    chron = by_name["daily-chronicle"]
    assert (chron.hour, chron.minute, chron.weekday) == (23, 59, None)
    assert chron.prompt  # body-as-prompt, non-empty
    summ = by_name["problem-summarizer"]
    assert (summ.hour, summ.minute, summ.weekday) == (4, 30, 0)  # Monday
    assert summ.prompt


def test_load_jobs_falls_back_to_hardcoded_when_no_scheduler_def(tmp_path):
    # The current pack carries no scheduler-kind frontmatter yet (pre-#484);
    # cron must keep firing on the hardcoded JOBS.
    (tmp_path / "skills").mkdir()
    assert crons.load_jobs(str(tmp_path / "skills")) == crons.JOBS
    assert crons.load_jobs(str(tmp_path / "missing")) == crons.JOBS


async def test_cron_fires_from_loaded_registry(db, tmp_path):
    skills_dir = tmp_path / "skills"
    _write_scheduler(skills_dir, "daily-chronicle", "59 23 * * *", "Schreibe.")
    deep = _FakeDeep()
    _baseline(db, "daily-chronicle")
    # No explicit jobs => CronRunner loads from the scheduler registry.
    runner = crons.CronRunner(
        db_path=db,
        deep=deep,
        skills_dir=str(skills_dir),
        context_window=32768,
    )
    await runner.tick(datetime(2026, 6, 12, 0, 5, tzinfo=_TZ))
    assert len(deep.turns) == 1
    _, text, _ = deep.turns[0]
    assert text.endswith("Schreibe.")


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
    assert deep.deleted == [(sid, "system")]


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


async def test_jobs_include_knowledge_night_run():
    by_name = {j.name: j for j in crons.JOBS}
    assert (
        by_name["knowledge-night-run"].hour,
        by_name["knowledge-night-run"].minute,
    ) == (2, 30)
    assert by_name["knowledge-night-run"].prompt == ""  # code job


async def test_knowledge_night_run_calls_all_steps(db, monkeypatch, tmp_path):
    calls = []

    async def fake_ingest(settings):
        calls.append("ingest")

    async def fake_drain(db_path, ollama_url):
        calls.append("drain")

    def fake_obsidian(settings, writer, uid):
        calls.append("obsidian")

    monkeypatch.setattr(crons, "run_ingest", fake_ingest)
    monkeypatch.setattr(crons.embed_worker, "drain", fake_drain)
    monkeypatch.setattr(crons, "_run_obsidian", fake_obsidian)
    monkeypatch.setattr(crons, "OkfWriter", lambda **kw: object())
    monkeypatch.setattr(crons, "PendingEmbeddingQueue", lambda p: object())

    class _Settings:
        solaris_db_path = db
        notes_dir = str(tmp_path)
        ollama_url = "http://x"
        default_uid = "household"

    job = crons.CronJob(name="knowledge-night-run", minute=30, hour=2)
    _baseline(db, "knowledge-night-run")
    runner = crons.CronRunner(
        db_path=db,
        deep=_FakeDeep(),
        skills_dir="",
        context_window=32768,
        jobs=(job,),
        ingest_settings=_Settings(),
    )
    await runner.tick(datetime(2026, 6, 12, 3, 0, tzinfo=_TZ))
    assert calls == ["ingest", "obsidian", "drain"]


async def test_knowledge_night_run_one_failing_step_does_not_abort_rest(
    db, monkeypatch, tmp_path
):
    calls = []

    async def fake_ingest(settings):
        raise RuntimeError("boom")

    async def fake_drain(db_path, ollama_url):
        calls.append("drain")

    def fake_obsidian(settings, writer, uid):
        calls.append("obsidian")

    monkeypatch.setattr(crons, "run_ingest", fake_ingest)
    monkeypatch.setattr(crons.embed_worker, "drain", fake_drain)
    monkeypatch.setattr(crons, "_run_obsidian", fake_obsidian)
    monkeypatch.setattr(crons, "OkfWriter", lambda **kw: object())
    monkeypatch.setattr(crons, "PendingEmbeddingQueue", lambda p: object())

    class _Settings:
        solaris_db_path = db
        notes_dir = str(tmp_path)
        ollama_url = "http://x"
        default_uid = "household"

    job = crons.CronJob(name="knowledge-night-run", minute=30, hour=2)
    _baseline(db, "knowledge-night-run")
    runner = crons.CronRunner(
        db_path=db,
        deep=_FakeDeep(),
        skills_dir="",
        context_window=32768,
        jobs=(job,),
        ingest_settings=_Settings(),
    )
    await runner.tick(datetime(2026, 6, 12, 3, 0, tzinfo=_TZ))
    # ingest raised, but obsidian + drain still ran.
    assert calls == ["obsidian", "drain"]


def _seed_session(db, sid, owner, *, ephemeral=0, maintenance=0, last_activity=None):
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO engine_sessions (id, owner_uid, ephemeral, maintenance,"
        " last_activity) VALUES (?, ?, ?, ?, COALESCE(?, datetime('now')))",
        (sid, owner, ephemeral, maintenance, last_activity),
    )
    conn.commit()
    conn.close()


def _seed_msg(db, sid, seq, role, content, created_at):
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO engine_messages (session_id, seq, role, content, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (sid, seq, role, content, created_at),
    )
    conn.commit()
    conn.close()


async def test_stenograph_distills_active_sessions_via_ephemeral_owner_turns(db):
    # An active resident session with a fresh conversation → one ephemeral deep
    # extraction turn owned by that resident, with the transcript inlined.
    _seed_session(db, "anna-chat", "anna", last_activity="2026-07-06 20:00:00")
    _seed_msg(
        db,
        "anna-chat",
        1,
        "user",
        "wir fahren im August nach Rom",
        "2026-07-06 19:00:00",
    )
    _seed_msg(db, "anna-chat", 2, "assistant", "schoen!", "2026-07-06 19:00:01")
    _baseline(db, "stenograph-watermark", "2026-07-06 00:00:00")
    deep = _FakeDeep()
    runner = _runner(db, deep)
    await runner._stenograph()
    assert len(deep.turns) == 1
    sid, text, effort = deep.turns[0]
    assert effort == "high"
    assert "wir fahren im August nach Rom" in text
    assert text.startswith(crons.compaction.STENOGRAPH_PREFIX[:20])
    # Ephemeral session owned by the resident, cleaned up.
    assert deep.created[0][0] == "anna"
    assert deep.created[0][1]["ephemeral"] is True
    assert deep.deleted == [(sid, "anna")]


async def test_stenograph_excludes_ephemeral_maintenance_and_stale(db):
    _seed_session(db, "eph", "anna", ephemeral=1, last_activity="2026-07-06 20:00:00")
    _seed_session(
        db, "maint", "anna", maintenance=1, last_activity="2026-07-06 20:00:00"
    )
    _seed_session(db, "stale", "anna", last_activity="2026-07-04 20:00:00")
    for sid in ("eph", "maint", "stale"):
        _seed_msg(db, sid, 1, "user", "etwas merkbares hier", "2026-07-06 19:00:00")
        _seed_msg(db, sid, 2, "assistant", "ok", "2026-07-06 19:00:01")
    _baseline(db, "stenograph-watermark", "2026-07-06 00:00:00")
    deep = _FakeDeep()
    await _runner(db, deep)._stenograph()
    assert deep.turns == []


async def test_stenograph_skips_trivially_short_slices(db):
    # A session with only ONE new turn is below the min-turns floor → skipped.
    _seed_session(db, "quiet", "anna", last_activity="2026-07-06 20:00:00")
    _seed_msg(db, "quiet", 1, "user", "hi", "2026-07-06 19:00:00")
    _baseline(db, "stenograph-watermark", "2026-07-06 00:00:00")
    deep = _FakeDeep()
    await _runner(db, deep)._stenograph()
    assert deep.turns == []


async def test_stenograph_advances_watermark_in_utc(db):
    _baseline(db, "stenograph-watermark", "2026-07-06 00:00:00")
    await _runner(db, _FakeDeep())._stenograph()
    mark = crons._last_run(db, "stenograph-watermark")
    # Advanced to a fresh UTC stamp (no timezone offset), not the old baseline.
    assert mark != "2026-07-06 00:00:00"
    datetime.strptime(mark, "%Y-%m-%d %H:%M:%S")  # parseable naive-UTC form


async def test_stenograph_first_run_baselines_to_last_24h(db):
    # No watermark row yet → the first run reads only ~24h back, not the whole
    # history, and still writes a watermark afterwards.
    _seed_session(db, "old", "anna", last_activity="2020-01-01 00:00:00")
    _seed_msg(db, "old", 1, "user", "uralt", "2020-01-01 00:00:00")
    _seed_msg(db, "old", 2, "assistant", "auch uralt", "2020-01-01 00:00:01")
    deep = _FakeDeep()
    await _runner(db, deep)._stenograph()
    assert deep.turns == []  # the ancient session predates the 24h baseline
    assert crons._last_run(db, "stenograph-watermark")


def test_render_transcript_head_truncates_to_cap(monkeypatch):
    monkeypatch.setattr(crons, "_STENOGRAPH_SLICE_CHARS", 40)
    msgs = [
        ("user", "aaaaaaaaaa"),
        ("assistant", "bbbbbbbbbb"),
        ("user", "cccccccccc"),
    ]
    out = crons._render_transcript(msgs)
    # Oldest lines dropped until under the cap; the newest survives.
    assert "cccccccccc" in out
    assert len(out) <= 40


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


async def test_compactor_never_forks_the_durable_household_session(db, monkeypatch):
    # The durable household session (#345) must NOT be compacted into a
    # `Fortsetzung` continuation by the nightly cron — that would surface as a
    # second "Zuhause" row (#419). Even stale + long, it is skipped in-place.
    from solaris_chat.engine import store

    hh = store.household_session_id("anna")
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO engine_sessions (id, owner_uid, input_tokens, output_tokens,"
        " last_activity) VALUES (?, 'anna', 30000, 2000, datetime('now', '-30 days'))",
        (hh,),
    )
    conn.commit()
    conn.close()

    compacted = []

    async def fake_compact(client, uid, session_id, *, context_window, force=False):
        compacted.append((session_id, force))
        return "continuation-1"

    monkeypatch.setattr(crons.compaction, "compact_session", fake_compact)
    job = crons.CronJob(name="chat-compactor", minute=15, hour=4)
    _baseline(db, "chat-compactor")
    runner = _runner(db, _FakeDeep(), jobs=(job,))
    await runner.tick(datetime(2026, 6, 12, 4, 20, tzinfo=_TZ))
    assert compacted == []

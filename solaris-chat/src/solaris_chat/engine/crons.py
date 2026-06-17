"""Engine night jobs — the Hermes cron registry, reborn as code.

The three background jobs (daily-chronicle, problem-summarizer,
chat-compactor) used to be registered into Hermes' jobs.json by the
post-deploy, which de-duped badly across upgrades (#332 follow-up). Here
they are defined in code — idempotent by construction — and run on the deep
profile (12b, thinks), matching the solaris-deep gateway they rode before.

Schedules are evaluated in local time (the household clock the prompts talk
about). A durable last-run stamp in solaris.db (`engine_cron_runs`) keys on
the fired slot, so a restart inside the cron minute never double-runs a job
and a restart spanning the slot fires it late instead of skipping the day.
"""

from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from solaris_chat import compaction
from solaris_chat.engine import store
from solaris_chat.logging import log

if TYPE_CHECKING:
    from solaris_chat.engine.client import EngineClient

_LOCAL_TZ = ZoneInfo("Europe/Berlin")
_POLL_S = 30.0
_CRON_UID = "system"

# A stale chat the nightly compactor picks up: untouched for a week and
# carrying enough transcript that compacting actually frees something.
_STALE_DAYS = 7
_STALE_MIN_USAGE = 0.5

CHRONICLE_PROMPT = (
    "Write today's family chronicle / journal entry for today. "
    "This is the unattended daily run — no resident is present, so "
    "do not ask anyone for highlights; compile from the day's "
    "ingested notes and household events you can see, and write a "
    "short honest entry (or skip a section) rather than inventing. "
    "Write it with note_write to journal/<YYYY>/<YYYY-MM-DD>.md."
)

PROBLEM_SUMMARIZER_PROMPT = (
    "Update the troubleshooting knowledge base. This is the unattended "
    "weekly run — no admin is present, so do not ask anyone for input. "
    "Search recent notes and past diagnostic threads with notes_search, "
    "extract resolved problem→indicators→solution sequences, and merge "
    "them into knowledge-base/troubleshooting.md with note_write "
    "(append new problems, update existing ones in place). If nothing "
    "new surfaced, leave the file untouched rather than inventing."
)


@dataclass(frozen=True)
class CronJob:
    name: str
    minute: int
    hour: int
    weekday: int | None = None  # 0=Monday … 6=Sunday; None = daily
    prompt: str = ""  # empty => a code job (the compactor)
    skill: str = ""  # skill id whose SKILL.md body rides the prompt


JOBS = (
    CronJob(
        name="daily-chronicle",
        minute=59,
        hour=23,
        prompt=CHRONICLE_PROMPT,
        skill="daily-chronicle",
    ),
    CronJob(
        name="problem-summarizer",
        minute=30,
        hour=4,
        weekday=0,
        prompt=PROBLEM_SUMMARIZER_PROMPT,
        skill="problem-summarizer",
    ),
    CronJob(name="chat-compactor", minute=15, hour=4),
)

# The compactor is a code job (empty prompt → _compact_stale), so it can't live
# as a prompt-bearing scheduler definition; it stays defined here and is always
# present in the loaded registry.
_CODE_JOBS = (CronJob(name="chat-compactor", minute=15, hour=4),)

_WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


def _parse_schedule(spec: str) -> tuple[int, int, int | None] | None:
    """A scheduler def's `schedule:` — a 5-field cron `min hour dom mon dow`,
    restricted to the shapes the runner supports (a single minute + hour, an
    optional single weekday; `*` elsewhere). Returns `(minute, hour, weekday)`
    or None when it isn't a shape we can fire."""
    parts = spec.split()
    if len(parts) != 5:
        return None
    minute, hour, dom, mon, dow = parts
    if dom != "*" or mon != "*":
        return None
    try:
        m, h = int(minute), int(hour)
    except ValueError:
        return None
    if not (0 <= m < 60 and 0 <= h < 24):
        return None
    weekday: int | None = None
    if dow != "*":
        weekday = _WEEKDAYS.get(dow.lower())
        if weekday is None:
            try:
                weekday = int(dow) % 7
            except ValueError:
                return None
    return m, h, weekday


def load_jobs(skills_dir: str) -> tuple[CronJob, ...]:
    """Build the cron registry from the scheduler-kind definitions in the pack
    (`schedule:` frontmatter + body-as-prompt), plus the code jobs.

    Falls back to the hardcoded `JOBS` when no scheduler-kind definition is
    present yet — the pack carries the `kind`/`schedule` frontmatter only after
    the #484 reorg, so cron keeps firing on the current pack meanwhile.
    """
    from solaris_chat import skills

    jobs: list[CronJob] = list(_CODE_JOBS)
    code_job_names = {j.name for j in _CODE_JOBS}
    for entry in skills.list_defs(skills_dir, "scheduler"):
        # The compactor is a scheduler-kind def for the editor/registry, but it
        # runs as the code job above — its body is not a prompt to feed an agent.
        if entry["id"] in code_job_names:
            continue
        one = skills.read_def(skills_dir, "scheduler", entry["id"])
        if one is None:
            continue
        meta, body = skills._split_frontmatter(one["raw"])
        parsed = _parse_schedule(meta.get("schedule", ""))
        if parsed is None or not body.strip():
            log.warning("engine.cron.skipped_invalid_scheduler", id=entry["id"])
            continue
        minute, hour, weekday = parsed
        jobs.append(
            CronJob(
                name=entry["id"],
                minute=minute,
                hour=hour,
                weekday=weekday,
                prompt=body.strip(),
                skill=entry["id"],
            )
        )
    has_scheduler_entry = any(j.prompt for j in jobs)
    return tuple(jobs) if has_scheduler_entry else JOBS


def _slot(job: CronJob, now: datetime) -> str | None:
    """The job's most recent due slot at/before `now` (ISO), or None when the
    job was never due in the lookback window."""
    candidate = now.replace(hour=job.hour, minute=job.minute, second=0, microsecond=0)
    for _ in range(8):  # at most a week + a day back (weekly jobs)
        if candidate <= now and (
            job.weekday is None or candidate.weekday() == job.weekday
        ):
            return candidate.isoformat()
        candidate -= timedelta(days=1)
    return None


def _last_run(db_path: str, name: str) -> str:
    try:
        with sqlite3.connect(db_path, timeout=10) as conn:
            row = conn.execute(
                "SELECT last_run FROM engine_cron_runs WHERE name = ?", (name,)
            ).fetchone()
        return row[0] if row else ""
    except sqlite3.Error:
        return ""


def _mark_run(db_path: str, name: str, slot: str) -> None:
    with sqlite3.connect(db_path, timeout=10) as conn:
        conn.execute(
            "INSERT INTO engine_cron_runs (name, last_run) VALUES (?, ?)"
            " ON CONFLICT(name) DO UPDATE SET last_run = excluded.last_run",
            (name, slot),
        )


def _skill_body(skills_dir: str, skill_id: str) -> str:
    """The skill markdown that used to ride the Hermes cron's `skills` list."""
    path = Path(skills_dir) / skill_id / "SKILL.md"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    if text.startswith("---"):
        end = text.find("---", 3)
        if end != -1:
            text = text[end + 3 :]
    return text.strip()


class CronRunner:
    """Polls the job table against the wall clock; runs due jobs once."""

    def __init__(
        self,
        *,
        db_path: str,
        deep: EngineClient,
        skills_dir: str,
        context_window: int,
        jobs: tuple[CronJob, ...] | None = None,
    ):
        self._db_path = db_path
        self._deep = deep
        self._skills_dir = skills_dir
        self._context_window = context_window
        self._jobs = jobs if jobs is not None else load_jobs(skills_dir)
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.get_event_loop().create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()

    async def _run(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception as e:  # noqa: BLE001 — the loop must outlive any hiccup
                log.error("engine.cron.error", error=str(e))
            await asyncio.sleep(_POLL_S)

    async def tick(self, now: datetime | None = None) -> None:
        now = now or datetime.now(_LOCAL_TZ)
        for job in self._jobs:
            slot = _slot(job, now)
            if slot is None:
                continue
            last = _last_run(self._db_path, job.name)
            if not last:
                # First-ever boot: baseline on the current slot instead of
                # back-running last night's job mid-day on a fresh install.
                _mark_run(self._db_path, job.name, slot)
                continue
            if last >= slot:
                continue
            _mark_run(self._db_path, job.name, slot)
            log.info("engine.cron.fired", job=job.name, slot=slot)
            if job.prompt:
                await self._run_agent_job(job)
            else:
                await self._compact_stale()

    async def _run_agent_job(self, job: CronJob) -> None:
        """One unattended agent turn on the deep profile, in an ephemeral
        session (the run's durable output is its tool effects, not the chat)."""
        prompt = job.prompt
        body = _skill_body(self._skills_dir, job.skill) if job.skill else ""
        if body:
            prompt = f"{body}\n\n---\n\n{prompt}"
        session_id = await self._deep.create_session(_CRON_UID, ephemeral=True)
        try:
            reply = await self._deep.chat(session_id, prompt, None, "high")
            log.info("engine.cron.done", job=job.name, reply_len=len(reply))
        finally:
            await self._deep.delete_session(session_id, _CRON_UID)

    async def _compact_stale(self) -> None:
        """The nightly chat-compactor: extract-then-compact stale, long chats
        via the same compaction path the per-turn hard cap uses (force=True —
        staleness, not cap pressure, selected them)."""
        cutoff = (datetime.now(_LOCAL_TZ) - timedelta(days=_STALE_DAYS)).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
        try:
            with sqlite3.connect(self._db_path, timeout=10) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT id, owner_uid, input_tokens, output_tokens"
                    " FROM engine_sessions"
                    " WHERE ephemeral = 0 AND last_activity < ?",
                    (cutoff,),
                ).fetchall()
        except sqlite3.Error as e:
            log.error("engine.cron.compact_query_failed", error=str(e))
            return
        for row in rows:
            # The durable household session (#345) is never forked into a
            # `Fortsetzung` continuation — that would surface as a second
            # "Zuhause" row (#419). Skip it; it stays in-place.
            if row["id"] == store.household_session_id(row["owner_uid"]):
                continue
            usage = compaction.usage_fraction(dict(row), self._context_window)
            if usage is None or usage < _STALE_MIN_USAGE:
                continue
            new_id = await compaction.compact_session(
                self._deep,
                row["owner_uid"],
                row["id"],
                context_window=self._context_window,
                force=True,
            )
            if new_id:
                # The continuation replaces the stale chat going forward; the
                # original transcript stays (never deleted), same as the
                # per-turn path.
                log.info(
                    "engine.cron.compacted",
                    session_id=row["id"],
                    continuation_id=new_id,
                )

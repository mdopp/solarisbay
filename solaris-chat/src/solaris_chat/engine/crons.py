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
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from solaris_chat import compaction
from solaris_chat.engine import store
from solaris_chat.engine.ingest import run_ingest
from solaris_chat.engine.ingest.runner import _run_obsidian
from solaris_chat.engine.knowledge import PendingEmbeddingQueue, embed_worker
from solaris_chat.engine.knowledge.writer import OkfWriter
from solaris_chat.logging import log

if TYPE_CHECKING:
    from solaris_chat.config import Settings
    from solaris_chat.engine.client import EngineClient

_LOCAL_TZ = ZoneInfo("Europe/Berlin")
_POLL_S = 30.0
_CRON_UID = "system"

# A stale chat the nightly compactor picks up: untouched for a week and
# carrying enough transcript that compacting actually frees something.
_STALE_DAYS = 7
_STALE_MIN_USAGE = 0.5

# The nightly Stenograph (#652): how many active sessions it distils per night,
# and the transcript slice cap fed to one extraction turn (head-truncated).
_STENOGRAPH_WATERMARK = "stenograph-watermark"
_STENOGRAPH_MAX_SESSIONS = 20
_STENOGRAPH_SLICE_CHARS = 8000
_STENOGRAPH_MIN_TURNS = 2

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
    CronJob(name="knowledge-night-run", minute=30, hour=2),
)

# Code jobs (empty prompt → dispatched by name, not fed to an agent) can't live
# as prompt-bearing scheduler definitions; they stay defined here and are always
# present in the loaded registry.
_CODE_JOBS = (
    CronJob(name="chat-compactor", minute=15, hour=4),
    CronJob(name="knowledge-night-run", minute=30, hour=2),
)

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


def _render_transcript(msgs: list[tuple[str, str]]) -> str:
    """Render `(role, content)` turns as `User:`/`Solaris:` lines, keeping the
    NEWEST within the slice cap (drop the oldest lines when it overflows)."""
    lines = [
        f"{'User' if role == 'user' else 'Solaris'}: {content}"
        for role, content in msgs
    ]
    while lines and sum(len(line) + 1 for line in lines) > _STENOGRAPH_SLICE_CHARS:
        lines.pop(0)
    return "\n".join(lines)


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
        ingest_settings: Settings | None = None,
    ):
        self._db_path = db_path
        self._deep = deep
        self._skills_dir = skills_dir
        self._context_window = context_window
        self._jobs = jobs if jobs is not None else load_jobs(skills_dir)
        self._ingest_settings = ingest_settings
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
            elif job.name == "knowledge-night-run":
                await self._knowledge_night_run()
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

    async def _knowledge_night_run(self) -> None:
        """The nightly knowledge pipeline (#652): re-ingest every source, run
        the bibliothekar hook (#653), re-ingest the vault, then drain embeddings.

        The ingest steps do synchronous sqlite writes + per-asset embedding work;
        run them in a worker thread with its own loop, NOT on the chat server's
        loop, so `/health` never starves during the run (the #586 lesson, same as
        the boot ingest in `__main__._bg_ingest`). Each step is isolated so one
        failing source doesn't abort the rest of the pipeline."""
        if self._ingest_settings is None:
            log.info("engine.night.skipped", reason="no_ingest_settings")
            return
        settings = self._ingest_settings

        # The Stenograph runs LIVE deep-client turns, so it stays on the chat
        # loop; the sqlite-heavy ingest pipeline below moves to a worker thread.
        try:
            await self._stenograph()
        except Exception as e:  # noqa: BLE001 — one step must not kill the rest.
            log.error("engine.night.stenograph_failed", error=str(e))

        async def _pipeline() -> None:
            try:
                await run_ingest(settings)
            except Exception as e:  # noqa: BLE001 — one step must not kill the rest.
                log.error("engine.night.ingest_failed", error=str(e))
            # The Bibliothekar (durable-fact consolidation) lands in #653; this is
            # its ordered slot in the pipeline until then.
            log.info("engine.night.bibliothekar_skipped")
            try:
                writer = OkfWriter(
                    db_path=settings.solaris_db_path,
                    notes_dir=settings.notes_dir,
                    embedding_queue=PendingEmbeddingQueue(settings.solaris_db_path),
                )
                _run_obsidian(settings, writer, settings.default_uid)
            except Exception as e:  # noqa: BLE001
                log.error("engine.night.obsidian_failed", error=str(e))
            try:
                await embed_worker.drain(settings.solaris_db_path, settings.ollama_url)
            except Exception as e:  # noqa: BLE001
                log.error("engine.night.embed_drain_failed", error=str(e))

        done = threading.Event()

        def _worker() -> None:
            try:
                asyncio.run(_pipeline())
            except Exception as e:  # noqa: BLE001 — the run must never crash the box.
                log.error("engine.night.thread_failed", error=str(e))
            finally:
                done.set()

        thread = threading.Thread(
            target=_worker, name="knowledge-night-run", daemon=True
        )
        thread.start()
        while not done.is_set():
            await asyncio.sleep(1.0)

    async def _stenograph(self) -> None:
        """Distil each active session's day into durable facts (#652).

        Reusing compaction's extract pass on the LIVE session would append the
        extraction turn to that session's durable history and mirror it to open
        tabs — unacceptable on the active household chat. So each session's new
        turns are rendered into the prompt and run through one extraction turn in
        an EPHEMERAL deep session owned by the source session's owner; the deep
        client sets `current_uid` from the session owner, so `fact_store` writes
        land under that resident's facts exactly as if they'd said "merk dir das".

        The watermark is a UTC timestamp: `engine_messages.created_at` /
        `last_activity` are sqlite UTC strings, while the cron slot stamps are
        local-ISO with an offset — comparing the two would silently drop rows.
        """
        now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        last = _last_run(self._db_path, _STENOGRAPH_WATERMARK)
        if not last:
            # First run: baseline to the last 24h rather than the whole history.
            last = (datetime.now(timezone.utc) - timedelta(days=1)).strftime(
                "%Y-%m-%d %H:%M:%S"
            )

        try:
            with sqlite3.connect(self._db_path, timeout=10) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT id, owner_uid FROM engine_sessions"
                    " WHERE ephemeral = 0 AND maintenance = 0 AND last_activity > ?"
                    " ORDER BY last_activity DESC LIMIT ?",
                    (last, _STENOGRAPH_MAX_SESSIONS),
                ).fetchall()
        except sqlite3.Error as e:
            log.error("engine.stenograph.query_failed", error=str(e))
            return

        skipped = 0
        for row in rows:
            slice_msgs = store.messages_since(self._db_path, row["id"], last)
            if len(slice_msgs) < _STENOGRAPH_MIN_TURNS:
                skipped += 1
                continue
            transcript = _render_transcript(slice_msgs)
            prompt = compaction.STENOGRAPH_PREFIX + transcript
            session_id = await self._deep.create_session(
                row["owner_uid"], ephemeral=True
            )
            try:
                reply = await self._deep.chat(session_id, prompt, None, "high")
                log.info(
                    "engine.stenograph.session",
                    source=row["id"],
                    owner=row["owner_uid"],
                    reply_len=len(reply),
                )
            finally:
                await self._deep.delete_session(session_id, row["owner_uid"])

        log.info(
            "engine.stenograph.done", distilled=len(rows) - skipped, skipped=skipped
        )
        # Advance the watermark only after the whole loop completes, so a crash
        # mid-run re-distils the same day rather than dropping sessions.
        _mark_run(self._db_path, _STENOGRAPH_WATERMARK, now_utc)

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

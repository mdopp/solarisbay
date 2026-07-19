"""Durable, owner-scoped import-job runner backed by ``engine_import_jobs``.

Takes the shape of the standalone tool's ``jobs.py`` (a runner registry keyed
by ``kind``, a progress-yielding generator, owner-scoped ``start``/``get``/
``cancel``/``latest_for`` and a boot-time ``resume``) but persists every job to
the ``engine_import_jobs`` SQLite table instead of per-job JSON files — so a
job survives a chat-server restart and a *different* device can re-attach to a
running import.

A job is a **server-side process**: the frontend only starts it and reconnects
by asking the server (``latest_for``/``get``). The row carries the runner input
(``payload``) and last progress snapshot (``progress``) as JSON-serialised TEXT;
on startup ``resume()`` re-spawns any row still ``running`` (or marks it
``interrupted`` when its ``kind`` has no registered runner). A runner is a
``factory(is_canceled) -> generator`` registered via ``@runner("kind")``; each
yielded dict updates ``progress`` (and, if it carries ``result``, the row's
final result rides ``progress['result']``).

The actual importer kinds (``google_takeout`` &c.) are dispatched through the
vendored ``google_takeout.REGISTRY`` and wired in #868; here the runner +
persistence + resume + a registered-kind lookup are the deliverable.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from collections.abc import Callable, Iterator
from typing import Any

from solaris_chat.logging import log

# The importer registry vendored in #863 — the set of kinds a job may dispatch
# to. Imported for the registered-kind lookup; kinds are wired to runners in
# #868.
from .google_takeout import REGISTRY as IMPORTER_REGISTRY


_PROGRESS_KEYS = ("pct", "message", "stage", "done", "total")

# kind -> factory-builder: fn(payload) -> factory(is_canceled) -> generator.
_RUNNERS: dict[
    str,
    Callable[
        [dict[str, Any]], Callable[[Callable[[], bool]], Iterator[dict[str, Any]]]
    ],
] = {}


def runner(kind: str):
    """Register a builder for a job kind: ``fn(payload) -> factory(is_canceled)``."""

    def deco(fn):
        _RUNNERS[kind] = fn
        return fn

    return deco


def registered_kind(kind: str) -> bool:
    """True when ``kind`` has a runner or is a known importer registry key."""
    return kind in _RUNNERS or kind in IMPORTER_REGISTRY


class JobRunner:
    """Owner-scoped durable import-job runner over ``engine_import_jobs``."""

    def __init__(self, db_path: str):
        self._db_path = db_path
        self._lock = threading.Lock()
        self._cancels: dict[str, threading.Event] = {}

    # -- persistence -------------------------------------------------------

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 5000")
        conn.execute("PRAGMA journal_mode = WAL")
        return conn

    def _set_status(self, jid: str, status: str, error: str | None = None) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE engine_import_jobs SET status = ?, error = ?,"
                " updated_at = datetime('now') WHERE id = ?",
                (status, error, jid),
            )

    def _set_progress(self, jid: str, progress: dict[str, Any]) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE engine_import_jobs SET progress = ?,"
                " updated_at = datetime('now') WHERE id = ?",
                (json.dumps(progress, ensure_ascii=False), jid),
            )

    @staticmethod
    def _snapshot(row: sqlite3.Row) -> dict[str, Any]:
        progress = json.loads(row["progress"] or "{}")
        return {
            "id": row["id"],
            "status": row["status"],
            "progress": progress,
            "result": progress.get("result"),
            "error": row["error"],
        }

    # -- public API --------------------------------------------------------

    def start(self, owner: str, kind: str, payload: dict[str, Any]) -> str:
        """Enqueue + spawn a job for ``owner``; ``kind`` selects the runner."""
        jid = uuid.uuid4().hex[:16]
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO engine_import_jobs (id, owner_uid, kind, status, payload)"
                " VALUES (?, ?, ?, 'running', ?)",
                (jid, owner, kind, json.dumps(payload, ensure_ascii=False)),
            )
        self._spawn(jid, kind, payload)
        return jid

    def get(self, jid: str, owner: str | None = None) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM engine_import_jobs WHERE id = ?", (jid,)
            ).fetchone()
        if row is None or (owner and row["owner_uid"] != owner):
            return None
        return self._snapshot(row)

    def cancel(self, jid: str, owner: str | None = None) -> bool:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT owner_uid, status FROM engine_import_jobs WHERE id = ?", (jid,)
            ).fetchone()
        if row is None or (owner and row["owner_uid"] != owner):
            return False
        with self._lock:
            ev = self._cancels.get(jid)
        if ev is not None:
            ev.set()
        return True

    def latest_for(self, owner: str) -> dict[str, Any] | None:
        """The owner's most recent job — what a fresh frontend re-attaches to."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM engine_import_jobs WHERE owner_uid = ?"
                " ORDER BY created_at DESC, id DESC LIMIT 1",
                (owner,),
            ).fetchone()
        if row is None:
            return None
        return {"jobId": row["id"], **self._snapshot(row)}

    def resume(self, owner: str | None = None) -> None:
        """Re-spawn (or mark ``interrupted``) rows still ``running`` at boot.

        Owner-scoped when ``owner`` is given (each engine client resumes its own
        owner's jobs, mirroring the timer/scheduler boot pattern)."""
        sql = (
            "SELECT id, kind, payload FROM engine_import_jobs WHERE status = 'running'"
        )
        args: tuple[Any, ...] = ()
        if owner is not None:
            sql += " AND owner_uid = ?"
            args = (owner,)
        with self._conn() as conn:
            rows = conn.execute(sql, args).fetchall()
        for row in rows:
            jid, kind = row["id"], row["kind"]
            with self._lock:
                if jid in self._cancels:
                    continue  # already live in this process
            if kind not in _RUNNERS:
                self._set_status(jid, "interrupted")
                continue
            payload = json.loads(row["payload"] or "{}")
            self._spawn(jid, kind, payload)

    # -- execution ---------------------------------------------------------

    def _spawn(self, jid: str, kind: str, payload: dict[str, Any]) -> None:
        factory = _RUNNERS[kind](payload)
        cancel = threading.Event()
        with self._lock:
            self._cancels[jid] = cancel
        threading.Thread(
            target=self._run,
            args=(jid, factory, cancel),
            name=f"import-job-{jid}",
            daemon=True,
        ).start()

    def _run(self, jid: str, factory, cancel: threading.Event) -> None:
        try:
            for ev in factory(cancel.is_set):
                if cancel.is_set():
                    self._set_status(jid, "interrupted")
                    return
                progress = {k: ev[k] for k in _PROGRESS_KEYS if k in ev}
                if "result" in ev:
                    progress["result"] = ev["result"]
                self._set_progress(jid, progress)
            if cancel.is_set():
                self._set_status(jid, "interrupted")
            else:
                self._set_status(jid, "done")
        except Exception as exc:  # noqa: BLE001 — a job failure never crashes the box
            log.error("engine.import_job.failed", job_id=jid, error=str(exc))
            self._set_status(jid, "failed", f"{type(exc).__name__}: {exc}")
        finally:
            with self._lock:
                self._cancels.pop(jid, None)

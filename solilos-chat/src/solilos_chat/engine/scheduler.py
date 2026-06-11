"""Timer/alarm/reminder scheduler — fires speaker announcements.

Timers live in solilos.db (`engine_timers`) so they survive a restart; one
asyncio loop polls the next pending row and fires it. Delivery is an HA
`assist_satellite.announce` to the Voice PE speaker (TTS rides HA's pipeline)
— HA stays the device tool, the schedule itself lives here, not in HA.
Fail-open: an unreachable HA marks the timer `failed` and logs; it never
kills the loop.
"""

from __future__ import annotations

import asyncio
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import aiohttp

from solilos_chat.logging import log

_POLL_S = 5.0


def _conn(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def add_timer(
    db_path: str,
    uid: str,
    *,
    duration_s: int | None = None,
    fire_at: str | None = None,
    kind: str = "timer",
    label: str = "",
    session_id: str = "",
) -> dict[str, Any]:
    """Insert a pending timer; returns its row as a dict."""
    if duration_s is not None:
        when = datetime.now(UTC) + timedelta(seconds=max(int(duration_s), 1))
    elif fire_at:
        when = datetime.fromisoformat(fire_at)
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
    else:
        raise ValueError("duration_s or fire_at required")
    timer_id = uuid.uuid4().hex[:12]
    with _conn(db_path) as conn:
        conn.execute(
            "INSERT INTO engine_timers (id, owner_uid, kind, label, fire_at,"
            " session_id) VALUES (?, ?, ?, ?, ?, ?)",
            (timer_id, uid, kind, label, when.isoformat(), session_id),
        )
    return {"id": timer_id, "kind": kind, "label": label, "fire_at": when.isoformat()}


def list_timers(db_path: str, uid: str) -> list[dict[str, Any]]:
    with _conn(db_path) as conn:
        rows = conn.execute(
            "SELECT id, kind, label, fire_at FROM engine_timers"
            " WHERE owner_uid = ? AND status = 'pending' ORDER BY fire_at",
            (uid,),
        ).fetchall()
    return [dict(r) for r in rows]


def cancel_timer(db_path: str, uid: str, timer_id: str) -> bool:
    with _conn(db_path) as conn:
        cur = conn.execute(
            "UPDATE engine_timers SET status = 'cancelled'"
            " WHERE id = ? AND owner_uid = ? AND status = 'pending'",
            (timer_id, uid),
        )
    return cur.rowcount > 0


class TimerScheduler:
    def __init__(self, db_path: str, hass_url: str, hass_token: str):
        self._db_path = db_path
        self._hass_url = hass_url.rstrip("/")
        self._hass_token = hass_token
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.get_event_loop().create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()

    async def _run(self) -> None:
        while True:
            try:
                await self._fire_due()
            except Exception as e:  # noqa: BLE001 — the loop must outlive any hiccup
                log.error("engine.scheduler.error", error=str(e))
            await asyncio.sleep(_POLL_S)

    async def _fire_due(self) -> None:
        now = datetime.now(UTC).isoformat()
        with _conn(self._db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM engine_timers WHERE status = 'pending' AND fire_at <= ?",
                (now,),
            ).fetchall()
        for row in rows:
            ok = await self._announce(dict(row))
            with _conn(self._db_path) as conn:
                conn.execute(
                    "UPDATE engine_timers SET status = ? WHERE id = ?",
                    ("fired" if ok else "failed", row["id"]),
                )
            log.info(
                "engine.timer.fired",
                timer_id=row["id"],
                kind=row["kind"],
                label=row["label"],
                delivered=ok,
            )

    async def _announce(self, timer: dict[str, Any]) -> bool:
        """Ring on the PE speaker via HA. True when HA accepted the call."""
        if not self._hass_url or not self._hass_token:
            return False
        label = timer.get("label") or ""
        kind = timer.get("kind") or "timer"
        text = {
            "timer": f"Der Timer {label} ist abgelaufen."
            if label
            else "Der Timer ist abgelaufen.",
            "alarm": f"Wecker: {label}" if label else "Es ist Zeit aufzustehen.",
            "reminder": f"Erinnerung: {label}" if label else "Erinnerung.",
        }.get(kind, f"{kind}: {label}")
        try:
            timeout = aiohttp.ClientTimeout(total=15)
            headers = {"Authorization": f"Bearer {self._hass_token}"}
            async with aiohttp.ClientSession(timeout=timeout) as client:
                async with client.post(
                    f"{self._hass_url}/api/services/assist_satellite/announce",
                    json={"message": text},
                    headers=headers,
                ) as resp:
                    return resp.status < 400
        except (aiohttp.ClientError, TimeoutError, OSError) as e:
            log.error("engine.timer.announce_failed", error=str(e))
            return False

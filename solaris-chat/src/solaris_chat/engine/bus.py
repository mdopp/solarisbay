"""In-process turn mirror — a session-scoped pub/sub for live browser sync.

A turn can originate in three places — a browser tab's `POST /api/chat/stream`,
another tab of the same person, or the voice facade — but only the originating
request streams it back. The mirror lets every OTHER open client of the SAME
session observe the turn near-live (#344): a tab subscribes to its open
session's events and replays the same `delta`/`tool`/`completed` shapes the
direct SSE already speaks, plus a `mirror_user` event for the inbound
transcript (voice STT or another tab's prompt).

Per-resident privacy (the `trace_store` D3 posture): a subscriber only sees a
session it owns, so the publish is uid-stamped and the subscribe is uid-scoped
— a session id alone never leaks another resident's turn. Single process,
single box: a plain in-memory fan-out, no Redis/broker (#341 decision).
"""

from __future__ import annotations

import asyncio
from typing import Any


class SessionBus:
    """Fan-out turn events to the open subscribers of one session.

    `subscribe(session_id, uid)` is an async generator of events; `publish`
    drops an event into every subscriber queue matching both the session and
    the owner uid. The originating request does NOT subscribe — it already has
    the direct stream — so a publisher never echoes to itself.
    """

    def __init__(self) -> None:
        # (session_id, uid) -> the live subscriber queues for that pair.
        self._subs: dict[tuple[str, str], set[asyncio.Queue[dict[str, Any]]]] = {}

    def publish(self, session_id: str, uid: str, event: dict[str, Any]) -> None:
        for q in self._subs.get((session_id, uid), set()):
            q.put_nowait(event)

    async def subscribe(self, session_id: str, uid: str):
        """Yield mirror events for `(session_id, uid)` until the client drops."""
        key = (session_id, uid)
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._subs.setdefault(key, set()).add(q)
        try:
            while True:
                yield await q.get()
        finally:
            subs = self._subs.get(key)
            if subs is not None:
                subs.discard(q)
                if not subs:
                    self._subs.pop(key, None)

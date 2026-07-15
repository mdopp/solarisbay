"""ServiceBay approval-event bridge — the BFF event side (ADR 0010, #811).

Per ADR 0010 the PHONE never subscribes to ServiceBay directly — Solaris
aggregates. ServiceBay emits new-pending-approval events on a server-server SSE
feed (`GET /napi/approvals/events`, servicebay#2268): a `data:`-framed
`NewApprovalEvent{type:"new-approval", id, kind, summary, created_at}`, plus
`{type:"connected"}` / `{type:"ping"}` keep-alive frames.

This is a persistent async task (started in `__main__` like `ApprovalPoller`)
that holds that SSE open and republishes each new-approval frame onto the Solaris
`EventBus` under the `servicebay` kind, scoped to `wartung_uid` (the household /
admin uid the approval-poller already cards into). The event then reaches the
paired admin device over the existing `/napi/portal/events` SSE (#806) — one bus,
one stream, no ServiceBay knowledge on the app.

Credentials: the SSE is `read`-scoped (servicebay#2268). Runs unattended, so it
reads the non-expiring read-only SB token (servicebay#2302, `sb_read_token_path`)
so it never 401-churns when the rotating deploy-time SB-MCP token lapses (#818),
falling back to that deploy-time token file when the read-token file is absent.

Fail-soft: an unreachable ServiceBay, a non-200, or a broken stream logs,
backs off, and reconnects — it never kills the loop and never republishes a
malformed frame. Dormant when `SB_API_URL` is unset.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import aiohttp

from solaris_chat.engine import store
from solaris_chat.engine.notify import EventBus, Notifier
from solaris_chat.engine.tools.mcp_tools import read_sb_token
from solaris_chat.logging import log

# The bus kind the app's `/napi/portal/events` pump forwards for SB events.
SERVICEBAY_KIND = "servicebay"
_EVENTS_PATH = "/napi/approvals/events"
_RECONNECT_S = 30.0
# No read timeout: an SSE stream is long-lived; only the connect/socket phases
# are bounded so a dead peer is noticed, not a quiet-but-alive stream torn down.
_TIMEOUT = aiohttp.ClientTimeout(total=None, sock_connect=20, sock_read=None)


def _to_bus_event(frame: dict[str, Any]) -> dict[str, Any] | None:
    """Map an SB `NewApprovalEvent` frame to the bus event payload, or None for a
    keep-alive (`connected`/`ping`) or a malformed frame."""
    if not isinstance(frame, dict) or frame.get("type") != "new-approval":
        return None
    approval_id = frame.get("id")
    if not isinstance(approval_id, str) or not approval_id:
        return None
    return {
        "id": approval_id,
        "kind": str(frame.get("kind") or ""),
        "summary": str(frame.get("summary") or ""),
    }


class SbApprovalEventBridge:
    def __init__(
        self,
        sb_api_url: str,
        sb_read_token_path: str,
        sb_mcp_token_path: str,
        bus: EventBus,
        wartung_uid: str,
        notifier: Notifier | None = None,
    ):
        self._base = sb_api_url.rstrip("/")
        self._read_token_path = sb_read_token_path
        self._token_path = sb_mcp_token_path
        self._bus = bus
        self._uid = wartung_uid
        self._notifier = notifier
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        if not self._base:
            log.info("engine.sb_events.disabled")
            return
        self._task = asyncio.get_event_loop().create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()

    async def _run(self) -> None:
        while True:
            try:
                await self._consume_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — the bridge must outlive any hiccup
                log.error("engine.sb_events.error", error=str(e))
            await asyncio.sleep(_RECONNECT_S)

    async def _consume_once(self) -> None:
        """Hold the SB SSE open and republish new-approval frames until it drops."""
        token = read_sb_token(self._read_token_path, self._token_path)
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        url = f"{self._base}{_EVENTS_PATH}"
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as client:
            async with client.get(url, headers=headers) as resp:
                if resp.status != 200:
                    log.warn("engine.sb_events.http", status=resp.status)
                    return
                async for raw in resp.content:
                    line = raw.decode("utf-8", "replace").strip()
                    if not line.startswith("data:"):
                        continue
                    try:
                        frame = json.loads(line[len("data:") :].strip())
                    except ValueError:
                        continue
                    await self._publish(frame)

    async def _publish(self, frame: dict[str, Any]) -> None:
        event = _to_bus_event(frame)
        if event is None:
            return
        self._bus.publish(self._uid, SERVICEBAY_KIND, event)
        log.info("engine.sb_events.republished", approval_id=event["id"])
        # When no SSE client is watching this uid the app is backgrounded, so the
        # approval reaches the phone only as a Web Push — same selective gate as
        # emit_chat (#843). The deep link opens the Wartung chat the approval cards
        # into (store.wartung_session_id); the SSE consumer opens the same target.
        if self._notifier is not None and not self._bus.has_subscriber(self._uid):
            url = f"/#/c/{store.wartung_session_id(self._uid)}"
            data = {"kind": SERVICEBAY_KIND, "id": event["id"], "url": url}
            body = event["summary"] or "Neue Freigabe angefragt."
            await self._notifier.push(self._uid, "Freigabe angefragt", body, data)

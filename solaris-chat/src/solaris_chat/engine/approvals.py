"""ServiceBay approval-request poller → Wartung approval-cards (Wartung P3, #790).

A persistent async task (started in `__main__` like `UpdatePoller` /
`HaStateWatcher`) that periodically asks ServiceBay's generic approval API which
cross-service approval requests are pending (`GET /api/approvals`). For each NEW
pending request it injects an approval action-card into the shared "Wartung"
admin chat (#785 `inject()` + the #787 action-card kind) so it appears in the
chat and — via the #713/#715 push path `inject()` already drives — pushes to the
admin's phone when no client is watching. The card carries [Approve] and [Deny]
buttons whose handlers (server.py, admin-gated) POST the operator's verdict back
to ServiceBay's `POST /api/approvals/{id}/approve` and `.../reject`.

Dedupe: a request is carded exactly once. The identity is the approval id (a
ServiceBay uuid), so the same pending request never re-cards every tick. Seen
ids persist in `wartung_seen_approvals` (migration 0023) so a restart doesn't
re-announce everything; the store degrades to "nothing seen" when the table is
missing, and that just means the next poll re-cards — never a crash.

Credentials — the UNATTENDED poll: `GET /api/approvals` requires a `read`-scope
Bearer (servicebay#2244). The poller runs in the background with no acting admin
session, so it reads the deploy-time SB-MCP token file (`sb_mcp_token_path`,
read+lifecycle+mutate) — the same token `UpdatePoller` uses for its unattended
poll, NOT a second credential. The [Approve]/[Deny] verdicts run under a LIVE
admin's session-exchanged token (their handlers live in `server.py`, admin-gated,
approve additionally destructive-gated).

Fail-open everywhere: an unreachable ServiceBay, a non-200, or malformed JSON
logs and skips this tick — it never kills the loop and never cards a phantom
request.
"""

from __future__ import annotations

import asyncio
import sqlite3
from typing import Any

import aiohttp

from solaris_chat.engine import store
from solaris_chat.engine.notify import EventBus, Notifier, inject
from solaris_chat.engine.tools.mcp_tools import read_token
from solaris_chat.logging import log

# The [Approve] / [Deny] button action ids, shared with the server-side handlers.
APPROVE_ACTION = "u790-approve-request"
DENY_ACTION = "u790-deny-request"

_POLL_S = 300.0
_TIMEOUT = aiohttp.ClientTimeout(total=20)
_LIST_PATH = "/api/approvals"


def _conn(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=10)
    # 10s so a poll tick that lands mid-ingest waits out the busy write path
    # instead of raising and skipping the tick (#835).
    conn.execute("PRAGMA busy_timeout = 10000")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def mark_seen(db_path: str, approval_id: str) -> bool:
    """Record `approval_id` as carded; return True iff it was NEW (worth carding).

    INSERT OR IGNORE against the unique id column: the first caller for an id
    inserts a row (rowcount 1 ⇒ new), a repeat is a no-op (rowcount 0 ⇒ seen).
    Degrades to True — "treat as new" — when the table is missing, so a not-yet-
    migrated box still cards (and simply re-cards next tick) rather than crash.
    """
    try:
        with _conn(db_path) as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO wartung_seen_approvals (approval_id) VALUES (?)",
                (approval_id,),
            )
            conn.commit()
            return cur.rowcount > 0
    except sqlite3.OperationalError:
        return True


async def submit_verdict(
    sb_api_url: str, token: str, approval_id: str, approve: bool
) -> tuple[bool, str]:
    """Deliver the operator's verdict to ServiceBay (#790): POST the
    mutate-scope token to `/api/approvals/{id}/approve` or `.../reject`.

    Returns `(ok, detail)` — `ok` iff ServiceBay returned 2xx (the verdict
    landed and any declared side effect ran); `detail` is a short human string
    for the chat. Fail-closed on the report side: a non-2xx or an unreachable
    ServiceBay returns `(False, <reason>)` so the card reflects that the verdict
    did NOT take, never a false success."""
    verb = "approve" if approve else "reject"
    base = sb_api_url.rstrip("/")
    url = f"{base}{_LIST_PATH}/{approval_id}/{verb}"
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as client:
            async with client.post(url, headers=headers) as resp:
                text = (await resp.text())[:400]
                if resp.status // 100 != 2:
                    log.warn(
                        "engine.approvals.verdict_http",
                        verb=verb,
                        status=resp.status,
                    )
                    return False, f"HTTP {resp.status}: {text}"
                return True, text or "ok"
    except (aiohttp.ClientError, TimeoutError, OSError) as e:
        log.warn("engine.approvals.verdict_failed", verb=verb, error=str(e))
        return False, str(e)


def _pending(approvals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The still-pending requests from the feed.

    `GET /api/approvals` returns ALL requests (pending/approved/rejected) newest
    first, so we filter to `status == "pending"` — an already-decided request
    must never surface as a fresh card."""
    out: list[dict[str, Any]] = []
    for a in approvals:
        if not isinstance(a, dict):
            continue
        if a.get("status") != "pending":
            continue
        if not a.get("id"):
            continue
        out.append(a)
    return out


def _label(request: dict[str, Any]) -> str:
    """Human one-liner for the card body: the requesting service + its title."""
    service = str(request.get("service") or "?")
    title = str(request.get("title") or "Freigabe erforderlich")
    return f"„{service}“: {title}"


def _card(approval_id: str, request: dict[str, Any]) -> dict[str, Any]:
    """The #787 action-card offering [Approve] / [Deny] for one request.

    Both buttons carry the approval `id` so their (admin-gated) handlers know
    which request to resolve; [Approve] is destructive (it runs the request's
    declared side effect on ServiceBay), so it is additionally confirm-gated by
    the endpoint. [Deny] cancels the proposal and needs no confirm."""
    description = request.get("description")
    body = _label(request)
    if isinstance(description, str) and description.strip():
        body = f"{body}\n{description.strip()}"
    return {
        "kind": "action",
        "title": "Freigabe angefragt",
        "body": body,
        "buttons": [
            {
                "label": "Approve",
                "action_id": APPROVE_ACTION,
                "destructive": True,
                "params": {"approval_id": approval_id},
            },
            {
                "label": "Deny",
                "action_id": DENY_ACTION,
                "params": {"approval_id": approval_id},
            },
        ],
    }


class ApprovalPoller:
    def __init__(
        self,
        db_path: str,
        sb_api_url: str,
        sb_mcp_token_path: str,
        bus: EventBus,
        wartung_uid: str,
        notifier: Notifier | None = None,
    ):
        self._db_path = db_path
        self._sb_api_url = sb_api_url.rstrip("/")
        self._token_path = sb_mcp_token_path
        self._bus = bus
        self._uid = wartung_uid
        self._notifier = notifier
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        # No SB control-plane base ⇒ nothing to poll; stay dormant rather than
        # loop on connection errors (mirrors UpdatePoller).
        if not self._sb_api_url:
            log.info("engine.approvals.disabled")
            return
        self._task = asyncio.get_event_loop().create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()

    async def _run(self) -> None:
        while True:
            try:
                await self.poll_once()
            except Exception as e:  # noqa: BLE001 — the loop must outlive any hiccup
                log.error("engine.approvals.error", error=str(e))
            await asyncio.sleep(_POLL_S)

    async def poll_once(self) -> int:
        """One poll: card each NEW pending approval request. Returns the number of
        cards injected this tick (0 when nothing new / unreachable)."""
        token = read_token(self._token_path)
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        feed = await self._get(f"{self._sb_api_url}{_LIST_PATH}", headers)
        pending = _pending((feed or {}).get("approvals") or [])

        session_id = store.wartung_session_id(self._uid)
        injected = 0
        for request in pending:
            approval_id = str(request["id"])
            if not mark_seen(self._db_path, approval_id):
                continue
            store.ensure_wartung_session(self._db_path, self._uid)
            await inject(
                self._db_path,
                self._bus,
                self._notifier,
                session_id,
                self._uid,
                f"Freigabe angefragt: {_label(request)}.",
                card=_card(approval_id, request),
            )
            log.info("engine.approvals.carded", approval_id=approval_id)
            injected += 1
        return injected

    async def _get(self, url: str, headers: dict[str, str]) -> dict[str, Any] | None:
        try:
            async with aiohttp.ClientSession(timeout=_TIMEOUT) as client:
                async with client.get(url, headers=headers) as resp:
                    if resp.status != 200:
                        log.warn("engine.approvals.http", url=url, status=resp.status)
                        return None
                    return await resp.json()
        except (aiohttp.ClientError, ValueError, TimeoutError, OSError) as e:
            log.warn("engine.approvals.fetch_failed", url=url, error=str(e))
            return None

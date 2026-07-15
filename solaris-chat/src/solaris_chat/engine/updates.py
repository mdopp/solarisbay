"""ServiceBay update poller → Wartung update-cards (Wartung P2b, #788).

A persistent async task (started in `__main__` like `TimerScheduler` /
`HaStateWatcher`) that periodically asks ServiceBay which deployed services have
a pending image update (`GET /api/system/stacks/image-updates`) or template
upgrade (`GET /api/system/templates/upgrades-pending`). For each NEW pending
update it injects an update action-card into the shared "Wartung" admin chat
(#785 `inject()` + the #787 action-card kind) so it appears in the chat and — via
the #713/#715 push path `inject()` already drives — pushes to the admin's phone
when no client is watching.

Dedupe: an update is carded exactly once. The identity is the update *target at
its new version* (`image:<service>:<registryDigest>` / `template:<name>:<v>`), so
the same pending update never re-cards every tick, but a *further* update (a new
digest / a higher schema version) does. Seen ids persist in
`wartung_seen_updates` (migration 0022) so a restart doesn't re-announce
everything; the store degrades to "nothing seen" when the table is missing, and
that just means the next poll re-cards — never a crash.

Credentials: the two endpoints require a `read`-scope Bearer. The poller runs in
the background with no acting admin session, so it reads the non-expiring
read-only SB token (servicebay#2302, `sb_read_token_path`) so it never 401-churns
when the rotating deploy-time SB-MCP token lapses (#818), falling back to that
deploy-time token file (`sb_mcp_token_path`) when the read-token file is absent.
The [Deploy] action the card offers runs under a live admin's session token (its
handler lives in `server.py`, admin+destructive-gated).

Fail-open everywhere: an unreachable ServiceBay, a non-200, or malformed JSON
logs and skips this tick — it never kills the loop and never cards a phantom
update.
"""

from __future__ import annotations

import asyncio
import sqlite3
from typing import Any

import aiohttp

from solaris_chat.engine import store
from solaris_chat.engine.notify import EventBus, Notifier, inject
from solaris_chat.engine.tools.mcp_tools import read_sb_token
from solaris_chat.logging import log

# The [Deploy] button's action id, shared with the server-side handler (#788).
DEPLOY_ACTION = "u788-deploy-update"

_POLL_S = 900.0
_TIMEOUT = aiohttp.ClientTimeout(total=20)
_IMAGE_PATH = "/api/system/stacks/image-updates"
_TEMPLATE_PATH = "/api/system/templates/upgrades-pending"


def _conn(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=10)
    # 10s so a poll tick that lands mid-ingest waits out the busy write path
    # instead of raising and skipping the tick (#835).
    conn.execute("PRAGMA busy_timeout = 10000")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def mark_seen(db_path: str, update_id: str) -> bool:
    """Record `update_id` as carded; return True iff it was NEW (worth carding).

    INSERT OR IGNORE against the unique id column: the first caller for an id
    inserts a row (rowcount 1 ⇒ new), a repeat is a no-op (rowcount 0 ⇒ seen).
    Degrades to True — "treat as new" — when the table is missing, so a not-yet-
    migrated box still cards (and simply re-cards next tick) rather than crash.
    """
    try:
        with _conn(db_path) as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO wartung_seen_updates (update_id) VALUES (?)",
                (update_id,),
            )
            conn.commit()
            return cur.rowcount > 0
    except sqlite3.OperationalError:
        return True


def _image_updates(services: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """(update_id, human label) for each service with a pending image update.

    Identity keys on the registry digest so a *further* image update re-cards;
    an item without `updateAvailable` or without both digests is skipped."""
    out: list[tuple[str, str]] = []
    for s in services:
        if not isinstance(s, dict) or not s.get("updateAvailable"):
            continue
        name = str(s.get("service") or "")
        digest = str(s.get("registryDigest") or "")
        if not name or not digest:
            continue
        out.append((f"image:{name}:{digest}", f"„{name}“ (neues Image)"))
    return out


def _template_upgrades(pending: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """(update_id, human label) for each service with a pending template upgrade.

    Identity keys on the target schema version so a higher version re-cards."""
    out: list[tuple[str, str]] = []
    for p in pending:
        if not isinstance(p, dict):
            continue
        name = str(p.get("name") or "")
        version = p.get("currentVersion")
        if not name or version is None:
            continue
        breaking = " — Breaking Change" if p.get("hasBreakingChange") else ""
        out.append(
            (
                f"template:{name}:{version}",
                f"„{name}“ (Schema v{version}{breaking})",
            )
        )
    return out


def _card(service: str, label: str) -> dict[str, Any]:
    """The #787 action-card offering [Deploy] for one service.

    The [Deploy] button carries `service` so its (admin+destructive-gated)
    handler knows what to install; the frontend confirm-gate + the endpoint's
    admin gate stop a bare/non-admin tap from ever reaching the handler."""
    return {
        "kind": "action",
        "title": "Update verfügbar",
        "body": f"Für {label} steht ein Update bereit.",
        "buttons": [
            {
                "label": "Deploy",
                "action_id": DEPLOY_ACTION,
                "destructive": True,
                "params": {"service": service},
            }
        ],
    }


class UpdatePoller:
    def __init__(
        self,
        db_path: str,
        sb_api_url: str,
        sb_read_token_path: str,
        sb_mcp_token_path: str,
        bus: EventBus,
        wartung_uid: str,
        notifier: Notifier | None = None,
    ):
        self._db_path = db_path
        self._sb_api_url = sb_api_url.rstrip("/")
        self._read_token_path = sb_read_token_path
        self._token_path = sb_mcp_token_path
        self._bus = bus
        self._uid = wartung_uid
        self._notifier = notifier
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        # No SB control-plane base ⇒ nothing to poll (the exchange base is unset
        # on a box without the update endpoints); stay dormant rather than loop
        # on connection errors.
        if not self._sb_api_url:
            log.info("engine.updates.disabled")
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
                log.error("engine.updates.error", error=str(e))
            await asyncio.sleep(_POLL_S)

    async def poll_once(self) -> int:
        """One poll: read both signals, card each NEW pending update. Returns the
        number of cards injected this tick (0 when nothing new / unreachable)."""
        token = read_sb_token(self._read_token_path, self._token_path)
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        images = await self._get(f"{self._sb_api_url}{_IMAGE_PATH}", headers)
        templates = await self._get(f"{self._sb_api_url}{_TEMPLATE_PATH}", headers)

        pending: list[tuple[str, str, str]] = []
        for update_id, label in _image_updates((images or {}).get("services") or []):
            pending.append((update_id, update_id.split(":")[1], label))
        for update_id, label in _template_upgrades(
            (templates or {}).get("pending") or []
        ):
            pending.append((update_id, update_id.split(":")[1], label))

        session_id = store.wartung_session_id(self._uid)
        injected = 0
        for update_id, service, label in pending:
            if not mark_seen(self._db_path, update_id):
                continue
            store.ensure_wartung_session(self._db_path, self._uid)
            await inject(
                self._db_path,
                self._bus,
                self._notifier,
                session_id,
                self._uid,
                f"Update verfügbar für {label}.",
                card=_card(service, label),
            )
            log.info("engine.updates.carded", update_id=update_id)
            injected += 1
        return injected

    async def _get(self, url: str, headers: dict[str, str]) -> dict[str, Any] | None:
        try:
            async with aiohttp.ClientSession(timeout=_TIMEOUT) as client:
                async with client.get(url, headers=headers) as resp:
                    if resp.status != 200:
                        log.warn("engine.updates.http", url=url, status=resp.status)
                        return None
                    return await resp.json()
        except (aiohttp.ClientError, ValueError, TimeoutError, OSError) as e:
            log.warn("engine.updates.fetch_failed", url=url, error=str(e))
            return None

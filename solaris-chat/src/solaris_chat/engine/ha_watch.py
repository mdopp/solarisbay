"""HA WebSocket state watcher — live card_state onto the event bus (#714).

A persistent async task (started in __main__ like `TimerScheduler`) that holds a
`/api/websocket` connection to Home Assistant, authenticates with the existing
`hass_token`, and subscribes to `state_changed` events. It is bounded to the
*pinned* entities: the union of every resident's pinned HA entities (from
`favorites_store`). On a change to a pinned entity it builds the fresh card-spec
(reusing `card_spec`) and publishes a `card_state` event to the uids that pinned
it — owner-scoped pins to the owner, the shared `household` pin to the
`HOUSEHOLD` sentinel (the SSE endpoint subscribes each client to both).

Resilient: any drop reconnects with capped backoff; the pinned set is re-derived
on connect and refreshed periodically so a new pin starts flowing without a
restart. HA stays the device tool — this only *reads* state changes.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import aiohttp

from solaris_chat import favorites_store
from solaris_chat.engine.notify import EventBus
from solaris_chat.engine.tools.ha import card_spec
from solaris_chat.favorites_store import HOUSEHOLD
from solaris_chat.logging import log

_BACKOFF_START_S = 2.0
_BACKOFF_MAX_S = 60.0
# Re-derive the pinned set this often so a new pin/unpin starts/stops flowing
# without a reconnect (a favorites change also re-derives on the next connect).
_PIN_REFRESH_S = 60.0

# Selective Web Push (#714): only these transitions push a notification while
# the app is closed (no open SSE client) — a cover/door opening or closing, a
# security/lock state flip. Lights/dimmers propagate over SSE only; they must
# NOT ring a phone. A small explicit allowlist by domain (device_class for
# cover, to keep e.g. an awning quiet).
_NOTEWORTHY_DOMAINS = frozenset({"lock", "binary_sensor"})
_NOTEWORTHY_COVER_CLASSES = frozenset({"garage", "door", "gate"})
_NOTEWORTHY_BINARY_CLASSES = frozenset({"door", "garage_door", "opening", "window"})


def _is_noteworthy(card: dict[str, Any]) -> bool:
    domain = card.get("domain")
    if domain == "cover":
        return card.get("device_class") in _NOTEWORTHY_COVER_CLASSES
    if domain == "binary_sensor":
        return card.get("device_class") in _NOTEWORTHY_BINARY_CLASSES
    return domain in _NOTEWORTHY_DOMAINS


class HaStateWatcher:
    def __init__(
        self,
        hass_url: str,
        hass_token: str,
        bus: EventBus,
        db_path: str,
        notifier: Any = None,
        native_watch: Any = None,
    ) -> None:
        self._hass_url = hass_url.rstrip("/")
        self._hass_token = hass_token
        self._bus = bus
        self._db_path = db_path
        self._notifier = notifier
        self._native_watch = native_watch
        self._task: asyncio.Task | None = None
        # entity_id -> owner uids that pinned it (HOUSEHOLD for the shared pin).
        self._owners: dict[str, set[str]] = {}
        self._refresh_at = 0.0
        # Live WS reachability, flipped on the loop thread; a plain bool read is
        # atomic in CPython so `status` needs no lock.
        self._connected = False

    @property
    def status(self) -> str:
        """Authoritative HA health for the start page (#729): 'disabled' when no
        url/token is configured, 'connected' while the WS is authenticated and
        live, 'disconnected' when configured but the WS is down / auth failing."""
        if not self._hass_url or not self._hass_token:
            return "disabled"
        return "connected" if self._connected else "disconnected"

    def start(self) -> None:
        if not self._hass_url or not self._hass_token:
            log.info("engine.ha_watch.disabled")
            return
        self._task = asyncio.get_event_loop().create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()

    def _refresh_pins(self) -> None:
        # Watched entities = web-pinned favorites ∪ per-device native watch-sets
        # (#810), so a native widget can watch an entity nobody favorited.
        owners = favorites_store.pinned_entity_owners(self._db_path)
        if self._native_watch is not None:
            for entity_id, uids in self._native_watch.native_watch_owners().items():
                owners.setdefault(entity_id, set()).update(uids)
        self._owners = owners

    async def _run(self) -> None:
        backoff = _BACKOFF_START_S
        while True:
            try:
                await self._connect_and_watch()
                backoff = _BACKOFF_START_S  # a clean return resets the backoff
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — the watcher must outlive any drop
                log.error("engine.ha_watch.error", error=str(e))
            finally:
                self._connected = False
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _BACKOFF_MAX_S)

    async def _connect_and_watch(self) -> None:
        ws_url = self._hass_url.replace("http", "ws", 1) + "/api/websocket"
        self._refresh_pins()
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(ws_url, heartbeat=30) as ws:
                await self._authenticate(ws)
                await ws.send_json(
                    {"id": 1, "type": "subscribe_events", "event_type": "state_changed"}
                )
                self._connected = True
                log.info("engine.ha_watch.connected", pinned=len(self._owners))
                loop = asyncio.get_event_loop()
                self._refresh_at = loop.time() + _PIN_REFRESH_S
                async for msg in ws:
                    if msg.type != aiohttp.WSMsgType.TEXT:
                        continue
                    if loop.time() >= self._refresh_at:
                        self._refresh_pins()
                        self._refresh_at = loop.time() + _PIN_REFRESH_S
                    self._on_message(json.loads(msg.data))

    async def _authenticate(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        # HA sends `auth_required`, we reply with the token, it answers
        # `auth_ok`/`auth_invalid`. An invalid token raises so the backoff loop
        # keeps retrying rather than silently watching nothing.
        await ws.receive_json()  # auth_required
        await ws.send_json({"type": "auth", "access_token": self._hass_token})
        reply = await ws.receive_json()
        if reply.get("type") != "auth_ok":
            raise RuntimeError(f"ha auth failed: {reply.get('type')}")

    def _on_message(self, msg: dict[str, Any]) -> None:
        if msg.get("type") != "event":
            return
        data = (msg.get("event") or {}).get("data") or {}
        entity_id = str(data.get("entity_id") or "")
        owners = self._owners.get(entity_id)
        if not owners:
            return
        new_state = data.get("new_state") or {}
        card = card_spec(
            entity_id,
            new_state.get("state"),
            new_state.get("attributes") or {},
            new_state.get("last_updated"),
        )
        if card is None:
            return
        noteworthy = self._notifier is not None and _is_noteworthy(card)
        for uid in owners:
            self._bus.publish(uid, "card_state", {"entity_id": entity_id, "card": card})
            # Push only when nobody is watching this uid live and the transition
            # is noteworthy; an SSE client already saw it (HOUSEHOLD reaches no
            # single phone, so it never pushes).
            if noteworthy and uid != HOUSEHOLD and not self._bus.has_subscriber(uid):
                asyncio.ensure_future(self._push(uid, card))

    async def _push(self, uid: str, card: dict[str, Any]) -> None:
        name = card.get("name") or card.get("entity_id")
        state = card.get("state") or ""
        await self._notifier.push(
            uid,
            str(name),
            str(state),
            {"kind": "card_state", "entity_id": card.get("entity_id")},
        )

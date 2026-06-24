"""HA area registry — rooms as first-class data (#535).

HA `/api/states` carry no area, so the engine had no room data: it could not
list the house's rooms and, lacking an area, fell back to surfacing raw
entity_ids. The area↔entity mapping only lives behind the HA **WebSocket** API
(`config/area_registry/list`, `config/device_registry/list`,
`config/entity_registry/list`), so this opens a short-lived authenticated WS,
pulls the three lists, and builds:

  * the area-name list (for "welche Räume hat das Haus"), and
  * an entity_id → area-name map (an entity's area is direct if set, else its
    device's area).

Cached with a TTL so a renamed/added room lands within minutes, and fail-open:
any WS error returns the last good snapshot (empty on first failure) so room
data only ever *enriches* the existing prompt, never breaks it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import aiohttp

from solaris_chat.logging import log

_TTL_S = 300.0
_TIMEOUT = aiohttp.ClientTimeout(total=10)


def _ws_url(hass_url: str) -> str:
    base = hass_url.rstrip("/")
    if base.startswith("https://"):
        base = "wss://" + base[len("https://") :]
    elif base.startswith("http://"):
        base = "ws://" + base[len("http://") :]
    return f"{base}/api/websocket"


@dataclass
class AreaSnapshot:
    """One resolved view of the HA area registry."""

    rooms: list[str] = field(default_factory=list)
    entity_area: dict[str, str] = field(default_factory=dict)

    def area_of(self, entity_id: str) -> str:
        return self.entity_area.get(entity_id, "")


class AreaRegistry:
    def __init__(self, hass_url: str, hass_token: str):
        self._url = hass_url
        self._token = hass_token
        self._snap = AreaSnapshot()
        self._fetched_at = 0.0
        self._fetched = False

    async def snapshot(self) -> AreaSnapshot:
        """Current area snapshot; the cached one while fresh, else a WS refresh.

        Returns the last good snapshot on any WS/auth error (stale beats empty,
        empty on first failure) — room data must never break the prompt."""
        if not self._url or not self._token:
            return self._snap
        # Guard on "have we ever fetched", not "is the result non-empty": an HA
        # with 0 configured areas yields an empty-but-valid snapshot that must
        # still satisfy the TTL, else every turn re-opens a WS (#546).
        if self._fetched and (time.time() - self._fetched_at) < _TTL_S:
            return self._snap
        try:
            snap = await self._fetch()
        except (aiohttp.ClientError, TimeoutError, OSError, ValueError) as e:
            log.warn("engine.areas.unreachable", error=str(e))
            return self._snap
        self._snap = snap
        self._fetched_at = time.time()
        self._fetched = True
        log.info(
            "engine.areas.refreshed",
            rooms=len(snap.rooms),
            mapped_entities=len(snap.entity_area),
        )
        return snap

    async def _fetch(self) -> AreaSnapshot:
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as client:
            async with client.ws_connect(_ws_url(self._url)) as ws:
                await self._auth(ws)
                areas = await self._command(ws, 1, "config/area_registry/list")
                devices = await self._command(ws, 2, "config/device_registry/list")
                entities = await self._command(ws, 3, "config/entity_registry/list")
        return _build_snapshot(areas, devices, entities)

    async def _auth(self, ws: aiohttp.ClientWebSocketResponse) -> None:
        first = await ws.receive_json()
        if first.get("type") != "auth_required":
            raise ValueError(f"unexpected ws greeting: {first.get('type')}")
        await ws.send_json({"type": "auth", "access_token": self._token})
        result = await ws.receive_json()
        if result.get("type") != "auth_ok":
            raise ValueError(f"ws auth failed: {result.get('type')}")

    @staticmethod
    async def _command(
        ws: aiohttp.ClientWebSocketResponse, msg_id: int, command: str
    ) -> list[dict[str, Any]]:
        await ws.send_json({"id": msg_id, "type": command})
        while True:
            msg = await ws.receive_json()
            if msg.get("id") != msg_id or msg.get("type") != "result":
                continue
            if not msg.get("success", False):
                raise ValueError(f"ws command {command} failed")
            result = msg.get("result")
            return result if isinstance(result, list) else []


def _build_snapshot(
    areas: list[dict[str, Any]],
    devices: list[dict[str, Any]],
    entities: list[dict[str, Any]],
) -> AreaSnapshot:
    """Resolve area names + an entity_id → area-name map from the three lists.

    An entity's area is its own `area_id` when set, else its device's
    `area_id`; either is dereferenced to the human area name."""
    name_by_id = {
        str(a.get("area_id")): str(a.get("name") or a.get("area_id") or "")
        for a in areas
        if a.get("area_id")
    }
    device_area = {
        str(d.get("id")): str(d.get("area_id") or "") for d in devices if d.get("id")
    }
    entity_area: dict[str, str] = {}
    for e in entities:
        eid = str(e.get("entity_id") or "")
        if not eid:
            continue
        area_id = e.get("area_id") or device_area.get(str(e.get("device_id") or ""))
        name = name_by_id.get(str(area_id or ""))
        if name:
            entity_area[eid] = name
    rooms = sorted(name_by_id.values())
    return AreaSnapshot(rooms=rooms, entity_area=entity_area)

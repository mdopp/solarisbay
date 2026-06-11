"""HA entity registry for prompt injection — the second-roundtrip killer.

Injecting the controllable-entity registry (id | name | area, NO live state)
into the system prompt lets the model call `ha_call_service` with the right
entity_id directly instead of spending an LLM pass on `ha_list_entities`
first — the same approach HA's own Assist uses. Live state is deliberately
absent: it goes stale in a cached prompt, and the soul rule "read live state
before answering" stays for state questions.

The block is sorted and stable so the KV prefix cache keeps hitting; a TTL
refresh picks up registry changes (new/renamed devices) within minutes.
"""

from __future__ import annotations

import time
from typing import Any

import aiohttp

from solilos_chat.logging import log

# Domains a household voice command can act on. Sensors etc. stay out — they
# inflate the prompt and are reachable via ha_list_entities when asked.
CONTROLLABLE_DOMAINS = (
    "light",
    "switch",
    "climate",
    "cover",
    "media_player",
    "scene",
    "script",
    "fan",
    "lock",
    "vacuum",
    "humidifier",
)

_TTL_S = 300.0


class EntityRegistry:
    def __init__(self, hass_url: str, hass_token: str):
        self._url = hass_url.rstrip("/")
        self._token = hass_token
        self._block = ""
        self._fetched_at = 0.0

    async def prompt_block(self) -> str:
        """The registry block for the system prompt; "" when HA is absent or
        unreachable (the prompt simply omits the device list — fail-open)."""
        if not self._url or not self._token:
            return ""
        if self._block and (time.time() - self._fetched_at) < _TTL_S:
            return self._block
        try:
            states = await self._fetch_states()
        except (aiohttp.ClientError, TimeoutError, OSError) as e:
            log.warn("engine.registry.unreachable", error=str(e))
            return self._block  # stale beats empty
        lines = []
        for s in states:
            entity_id = str(s.get("entity_id") or "")
            domain = entity_id.split(".", 1)[0]
            if domain not in CONTROLLABLE_DOMAINS:
                continue
            attrs = s.get("attributes") or {}
            name = str(attrs.get("friendly_name") or entity_id)
            area = str(attrs.get("area") or "")
            lines.append(f"{entity_id} | {name} | {area}".rstrip(" |"))
        lines.sort()
        self._block = (
            "Geräte (entity_id | Name | Raum):\n" + "\n".join(lines) if lines else ""
        )
        self._fetched_at = time.time()
        log.info("engine.registry.refreshed", entities=len(lines))
        return self._block

    async def _fetch_states(self) -> list[dict[str, Any]]:
        timeout = aiohttp.ClientTimeout(total=10)
        headers = {"Authorization": f"Bearer {self._token}"}
        async with aiohttp.ClientSession(timeout=timeout) as client:
            async with client.get(f"{self._url}/api/states", headers=headers) as resp:
                resp.raise_for_status()
                body = await resp.json()
        return body if isinstance(body, list) else []

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

from solaris_chat.logging import log

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

# Real HA service names per domain, so the model emits e.g. cover.open_cover
# (not the guessed cover.open that 400'd, #379) without a separate roundtrip.
# Kept as a compact per-domain legend appended once, not repeated per entity.
_DOMAIN_SERVICES = {
    "light": "turn_on/turn_off",
    "switch": "turn_on/turn_off",
    "climate": "set_temperature/set_hvac_mode",
    "cover": "open_cover/close_cover/stop_cover",
    "media_player": "media_play/media_pause/media_stop/volume_set",
    "scene": "turn_on",
    "script": "turn_on",
    "fan": "turn_on/turn_off/set_percentage",
    "lock": "lock/unlock",
    "vacuum": "start/pause/return_to_base",
    "humidifier": "turn_on/turn_off/set_humidity",
}
# cover.set_cover_position is only valid when SUPPORT_SET_POSITION (bit 2 = 4)
# is in supported_features; appended to the cover legend when any cover has it.
_COVER_SET_POSITION = 4


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
        domains: set[str] = set()
        cover_set_position = False
        for s in states:
            entity_id = str(s.get("entity_id") or "")
            domain = entity_id.split(".", 1)[0]
            if domain not in CONTROLLABLE_DOMAINS:
                continue
            attrs = s.get("attributes") or {}
            name = str(attrs.get("friendly_name") or entity_id)
            area = str(attrs.get("area") or "")
            line = f"{entity_id} | {name} | {area}".rstrip(" |")
            domains.add(domain)
            if domain == "cover":
                # device_class distinguishes a garage cover (confirm-first) from
                # an ordinary blind/shade (act) — both are domain=cover, so the
                # safety rule can only key on it if it's surfaced per entity.
                device_class = str(attrs.get("device_class") or "")
                if device_class:
                    line += f" | {device_class}"
                features = attrs.get("supported_features") or 0
                if isinstance(features, int) and features & _COVER_SET_POSITION:
                    cover_set_position = True
            lines.append(line)
        lines.sort()
        self._block = (
            "Geräte (entity_id | Name | Raum[ | Geräteklasse bei cover]):\n"
            + "\n".join(lines)
            + "\n"
            + self._actions_legend(domains, cover_set_position)
            if lines
            else ""
        )
        self._fetched_at = time.time()
        log.info("engine.registry.refreshed", entities=len(lines))
        return self._block

    @staticmethod
    def _actions_legend(domains: set[str], cover_set_position: bool) -> str:
        legend = ["Aktionen (ha_call_service domain.service):"]
        for domain in CONTROLLABLE_DOMAINS:
            if domain not in domains:
                continue
            services = _DOMAIN_SERVICES[domain]
            if domain == "cover" and cover_set_position:
                services += "/set_cover_position"
            legend.append(f"{domain}: {services}")
        return "\n".join(legend)

    async def _fetch_states(self) -> list[dict[str, Any]]:
        timeout = aiohttp.ClientTimeout(total=10)
        headers = {"Authorization": f"Bearer {self._token}"}
        async with aiohttp.ClientSession(timeout=timeout) as client:
            async with client.get(f"{self._url}/api/states", headers=headers) as resp:
                resp.raise_for_status()
                body = await resp.json()
        return body if isinstance(body, list) else []

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

from solaris_chat.engine.areas import AreaRegistry
from solaris_chat.logging import log

# Read-only domains we advertise for ON-DEMAND discovery instead of packing
# their (often hundreds of) entities into every prompt: the prompt carries the
# actionable devices in full, plus a legend of which sensor device_classes /
# read-only domains exist, and the model fetches the specific ones it needs with
# ha_list_entities(device_class=… / domain=…). Box-observed without this the
# model guesses a non-existent climate.* and gives up rather than querying.
QUERYABLE_READONLY_DOMAINS = ("sensor", "binary_sensor")

# Domains a household voice command can act on — packed into the prompt in full
# so actions resolve in one pass. Everything else is discovered via the legend.
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
    "media_player": "play_media/media_play/media_pause/media_stop/media_next_track/media_previous_track/volume_set",
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
        self._areas = AreaRegistry(hass_url, hass_token)
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
        areas = await self._areas.snapshot()
        lines = []
        domains: set[str] = set()
        cover_set_position = False
        # Discovery legend inputs: which read-only domains exist and, for sensors,
        # which device_classes — so the model knows what it can fetch on demand.
        readonly_domains: set[str] = set()
        sensor_classes: set[str] = set()
        for s in states:
            entity_id = str(s.get("entity_id") or "")
            domain = entity_id.split(".", 1)[0]
            attrs = s.get("attributes") or {}
            device_class = str(attrs.get("device_class") or "")
            if domain not in CONTROLLABLE_DOMAINS:
                # Not actionable — keep it out of the prompt, just record that it
                # exists so the legend can point the model at a targeted query.
                if domain in QUERYABLE_READONLY_DOMAINS:
                    readonly_domains.add(domain)
                    if device_class:
                        sensor_classes.add(device_class)
                continue
            name = str(attrs.get("friendly_name") or entity_id)
            area = areas.area_of(entity_id) or str(attrs.get("area") or "")
            line = f"{entity_id} | {name} | {area}".rstrip(" |")
            domains.add(domain)
            if domain == "cover":
                # device_class distinguishes a garage cover (confirm-first) from
                # an ordinary blind/shade (act) — both are domain=cover, so the
                # safety rule can only key on it if it's surfaced per entity.
                if device_class:
                    line += f" | {device_class}"
                features = attrs.get("supported_features") or 0
                if isinstance(features, int) and features & _COVER_SET_POSITION:
                    cover_set_position = True
            lines.append(line)
        lines.sort()
        parts = [
            self._rooms_block(areas.rooms),
            "Steuerbare Geräte (entity_id | Name | Raum[ | Geräteklasse bei cover]):\n"
            + "\n".join(lines),
            self._actions_legend(domains, cover_set_position),
            self._discovery_legend(sorted(readonly_domains), sorted(sensor_classes)),
        ]
        self._block = "\n".join(p for p in parts if p) if (lines or areas.rooms) else ""
        self._fetched_at = time.time()
        log.info(
            "engine.registry.refreshed",
            entities=len(lines),
            sensor_classes=len(sensor_classes),
        )
        return self._block

    @staticmethod
    def _rooms_block(rooms: list[str]) -> str:
        """The house's rooms, so "welche Räume hat das Haus" is answerable from
        the prompt directly (HA states carry no area, #535)."""
        if not rooms:
            return ""
        return "Räume im Haus: " + ", ".join(rooms)

    @staticmethod
    def _discovery_legend(domains: list[str], classes: list[str]) -> str:
        """Tell the model what read-only devices exist beyond the actionable list
        and how to pull the specific ones it needs — so we don't pack hundreds of
        sensors into every prompt and the model still answers e.g. room
        temperature, energy or battery questions in one targeted query."""
        if not domains and not classes:
            return ""
        legend = [
            "Weitere Geräte sind nur lesbar und NICHT oben gelistet — bei Bedarf",
            'gezielt abrufen mit ha_list_entities (z.B. device_class="temperature"',
            "für die Raumtemperatur, oder domain=… / name=…):",
        ]
        if classes:
            legend.append("  Sensor-device_class: " + ", ".join(classes))
        if domains:
            legend.append("  read-only domains: " + ", ".join(domains))
        return "\n".join(legend)

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

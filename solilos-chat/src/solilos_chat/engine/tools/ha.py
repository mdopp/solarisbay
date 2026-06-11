"""Home Assistant tools — control, state, discovery.

The injected entity registry (registry.py) makes `ha_call_service` a one-pass
action; `ha_get_state`/`ha_list_entities` stay for state questions (the soul
rule: read live state, never answer device questions from memory).

The domain/service validation is ported from the Hermes tool it replaces:
the names are interpolated into `/api/services/{domain}/{service}`, so the
regex blocks path traversal and the blocklist keeps arbitrary-code domains
(shell_command & friends) unreachable no matter what the model asks for.
"""

from __future__ import annotations

import json
import re
from typing import Any

import aiohttp

from solilos_chat.engine.tools import Tool

_NAME_RE = re.compile(r"^[a-z_][a-z0-9_]*$")
_BLOCKED_DOMAINS = frozenset(
    {"shell_command", "python_script", "pyscript", "hassio", "homeassistant"}
)
_TIMEOUT = aiohttp.ClientTimeout(total=15)


def build_ha_tools(hass_url: str, hass_token: str) -> list[Tool]:
    url = hass_url.rstrip("/")
    headers = {
        "Authorization": f"Bearer {hass_token}",
        "Content-Type": "application/json",
    }

    async def call_service(args: dict[str, Any]) -> str:
        domain = str(args.get("domain") or "")
        service = str(args.get("service") or "")
        entity_id = str(args.get("entity_id") or "")
        if not _NAME_RE.match(domain) or not _NAME_RE.match(service):
            return '{"error": "invalid domain or service name"}'
        if domain in _BLOCKED_DOMAINS:
            return f'{{"error": "domain {domain} is not allowed"}}'
        payload: dict[str, Any] = {"entity_id": entity_id} if entity_id else {}
        data = args.get("data")
        if isinstance(data, dict):
            payload.update(data)
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as client:
            async with client.post(
                f"{url}/api/services/{domain}/{service}",
                json=payload,
                headers=headers,
            ) as resp:
                if resp.status >= 400:
                    detail = (await resp.text())[:200]
                    return json.dumps({"error": f"HA {resp.status}: {detail}"})
        return json.dumps({"success": True, "service": f"{domain}.{service}"})

    async def get_state(args: dict[str, Any]) -> str:
        entity_id = str(args.get("entity_id") or "")
        if not re.match(r"^[a-z_]+\.[a-z0-9_]+$", entity_id):
            return '{"error": "invalid entity_id"}'
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as client:
            async with client.get(
                f"{url}/api/states/{entity_id}", headers=headers
            ) as resp:
                if resp.status == 404:
                    return json.dumps({"error": f"unknown entity: {entity_id}"})
                resp.raise_for_status()
                body = await resp.json()
        return json.dumps(
            {
                "entity_id": entity_id,
                "state": body.get("state"),
                "attributes": {
                    k: v
                    for k, v in (body.get("attributes") or {}).items()
                    if k
                    in (
                        "friendly_name",
                        "unit_of_measurement",
                        "temperature",
                        "current_temperature",
                        "brightness",
                        "media_title",
                    )
                },
            },
            ensure_ascii=False,
        )

    async def list_entities(args: dict[str, Any]) -> str:
        domain = str(args.get("domain") or "")
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as client:
            async with client.get(f"{url}/api/states", headers=headers) as resp:
                resp.raise_for_status()
                states = await resp.json()
        out = []
        for s in states:
            eid = str(s.get("entity_id") or "")
            if domain and not eid.startswith(f"{domain}."):
                continue
            name = (s.get("attributes") or {}).get("friendly_name") or eid
            out.append({"entity_id": eid, "state": s.get("state"), "name": name})
            if len(out) >= 100:
                break
        return json.dumps(out, ensure_ascii=False)

    return [
        Tool(
            name="ha_call_service",
            description=(
                "Steuert ein Home-Assistant-Gerät. Nutze die entity_id aus der"
                " Geräteliste im Systemprompt."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "domain": {"type": "string", "description": "z.B. light, climate"},
                    "service": {
                        "type": "string",
                        "description": "z.B. turn_on, turn_off, set_temperature",
                    },
                    "entity_id": {"type": "string"},
                    "data": {
                        "type": "object",
                        "description": 'optional, z.B. {"temperature": 21}',
                    },
                },
                "required": ["domain", "service", "entity_id"],
            },
            handler=call_service,
        ),
        Tool(
            name="ha_get_state",
            description="Liest den Live-Zustand einer Entity.",
            parameters={
                "type": "object",
                "properties": {"entity_id": {"type": "string"}},
                "required": ["entity_id"],
            },
            handler=get_state,
        ),
        Tool(
            name="ha_list_entities",
            description=(
                "Listet Entities mit Live-Zustand, optional nach Domain gefiltert"
                " — für Zustandsfragen über mehrere Geräte."
            ),
            parameters={
                "type": "object",
                "properties": {"domain": {"type": "string"}},
            },
            handler=list_entities,
        ),
    ]

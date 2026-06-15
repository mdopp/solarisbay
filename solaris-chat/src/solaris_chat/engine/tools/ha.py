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
from datetime import UTC, datetime, timedelta
from typing import Any

import aiohttp

from solaris_chat.engine.tools import Tool

_NAME_RE = re.compile(r"^[a-z_][a-z0-9_]*$")
_ENTITY_RE = re.compile(r"^[a-z_]+\.[a-z0-9_]+$")
_BLOCKED_DOMAINS = frozenset(
    {"shell_command", "python_script", "pyscript", "hassio", "homeassistant"}
)
_TIMEOUT = aiohttp.ClientTimeout(total=15)
# Domains that "list/run scripts, automations, scenes" operates on (#370).
_RUNNABLE_DOMAINS = ("scene", "script", "automation")
# Run service per runnable domain: scenes/scripts turn_on, automations trigger.
_RUN_SERVICE = {"scene": "turn_on", "script": "turn_on", "automation": "trigger"}
# Some domains name their actions verb_<domain> rather than the bare verb the
# model tends to guess (cover has no `open`, only `open_cover`) — map the
# known-safe aliases so a natural "open" reaches the right HA service (#379).
_SERVICE_ALIASES = {
    "cover": {"open": "open_cover", "close": "close_cover", "stop": "stop_cover"},
}
_HISTORY_DEFAULT_DAYS = 7
_HISTORY_MAX_TRANSITIONS = 20


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


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
        service = _SERVICE_ALIASES.get(domain, {}).get(service, service)
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

    async def _resolve_entity_id(ref: str) -> str:
        """A literal entity_id passes through; otherwise match a friendly_name
        (case-insensitive, exact then substring) against /api/states. "" on
        no match — the caller turns that into a model-readable error."""
        ref = ref.strip()
        if _ENTITY_RE.match(ref):
            return ref
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as client:
            async with client.get(f"{url}/api/states", headers=headers) as resp:
                resp.raise_for_status()
                states = await resp.json()
        wanted = ref.lower()
        substr = ""
        for s in states:
            eid = str(s.get("entity_id") or "")
            name = str((s.get("attributes") or {}).get("friendly_name") or "")
            if name.lower() == wanted:
                return eid
            if not substr and wanted and wanted in name.lower():
                substr = eid
        return substr

    async def get_state_history(args: dict[str, Any]) -> str:
        ref = str(args.get("entity") or args.get("entity_id") or "")
        entity_id = await _resolve_entity_id(ref)
        if not entity_id:
            return json.dumps({"error": f"no entity matched: {ref}"})
        try:
            days = int(args.get("days") or _HISTORY_DEFAULT_DAYS)
        except (TypeError, ValueError):
            days = _HISTORY_DEFAULT_DAYS
        days = max(1, min(days, 30))
        end = datetime.now(UTC)
        start = end - timedelta(days=days)
        params = {
            "filter_entity_id": entity_id,
            "end_time": end.isoformat(),
            "minimal_response": "true",
        }
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as client:
            async with client.get(
                f"{url}/api/history/period/{start.isoformat()}",
                params=params,
                headers=headers,
            ) as resp:
                if resp.status == 404:
                    return json.dumps({"error": f"unknown entity: {entity_id}"})
                resp.raise_for_status()
                body = await resp.json()
        # HA returns [[ {state, last_changed}, ... ]] — one list per entity.
        series = body[0] if body and isinstance(body[0], list) else []
        transitions = []
        prev = None
        for point in series:
            state = point.get("state")
            when = point.get("last_changed") or point.get("last_updated")
            if state == prev or not when:
                continue
            transitions.append({"state": state, "since": when})
            prev = state
        # Durations: each transition lasts until the next (the last is "now").
        bounds = [t["since"] for t in transitions] + [end.isoformat()]
        for i, t in enumerate(transitions):
            t["duration_s"] = round(
                (_parse(bounds[i + 1]) - _parse(bounds[i])).total_seconds()
            )
        recent = transitions[-_HISTORY_MAX_TRANSITIONS:]
        return json.dumps(
            {"entity_id": entity_id, "days": days, "transitions": recent},
            ensure_ascii=False,
        )

    async def list_runnable(args: dict[str, Any]) -> str:
        domain = str(args.get("domain") or "")
        domains = (domain,) if domain in _RUNNABLE_DOMAINS else _RUNNABLE_DOMAINS
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as client:
            async with client.get(f"{url}/api/states", headers=headers) as resp:
                resp.raise_for_status()
                states = await resp.json()
        out = []
        for s in states:
            eid = str(s.get("entity_id") or "")
            if eid.split(".", 1)[0] not in domains:
                continue
            name = (s.get("attributes") or {}).get("friendly_name") or eid
            out.append({"entity_id": eid, "name": name})
        return json.dumps(out, ensure_ascii=False)

    async def run_runnable(args: dict[str, Any]) -> str:
        ref = str(args.get("entity") or args.get("entity_id") or "")
        entity_id = await _resolve_entity_id(ref)
        domain = entity_id.split(".", 1)[0] if entity_id else ""
        if domain not in _RUNNABLE_DOMAINS:
            return json.dumps({"error": f"not a script/automation/scene: {ref}"})
        return await call_service(
            {"domain": domain, "service": _RUN_SERVICE[domain], "entity_id": entity_id}
        )

    return [
        Tool(
            name="ha_call_service",
            description=(
                "Steuert ein Home-Assistant-Gerät. Nutze die entity_id aus der"
                " Geräteliste im Systemprompt. Service-Namen sind HA-spezifisch:"
                " light/switch/climate -> turn_on/turn_off (climate auch"
                " set_temperature); cover (Garage/Rollladen/Tor) ->"
                " open_cover/close_cover/stop_cover; lock -> lock/unlock."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "domain": {"type": "string", "description": "z.B. light, climate"},
                    "service": {
                        "type": "string",
                        "description": (
                            "HA-Service, z.B. turn_on, turn_off, set_temperature;"
                            " cover: open_cover/close_cover/stop_cover"
                        ),
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
        Tool(
            name="ha_state_history",
            description=(
                "Wann war eine Entity zuletzt an/aus? Liefert die letzten"
                " Zustandswechsel mit Zeit und Dauer. Akzeptiert entity_id oder"
                " Gerätenamen; Fenster standardmäßig 7 Tage."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "entity": {
                        "type": "string",
                        "description": "entity_id oder Gerätename",
                    },
                    "days": {"type": "integer", "description": "Fenster, 1-30"},
                },
                "required": ["entity"],
            },
            handler=get_state_history,
        ),
        Tool(
            name="ha_list_scenes_scripts",
            description=(
                "Listet verfügbare Szenen, Skripte und Automationen, optional"
                " nach Domain (scene/script/automation) gefiltert."
            ),
            parameters={
                "type": "object",
                "properties": {"domain": {"type": "string"}},
            },
            handler=list_runnable,
        ),
        Tool(
            name="ha_run_scene_script",
            description=(
                "Startet eine Szene, ein Skript oder eine Automation."
                " Akzeptiert entity_id oder Namen (z.B. 'Schlafenszeit-Routine')."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "entity": {
                        "type": "string",
                        "description": "entity_id oder Name",
                    },
                },
                "required": ["entity"],
            },
            handler=run_runnable,
        ),
    ]

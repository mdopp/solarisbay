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

import contextvars
import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any

import aiohttp

from solaris_chat.engine.tools import Tool

# Per-turn sink for the read-only state cards a turn surfaces (#475). Each HA
# state read appends a card-spec here; the engine loop drains it at turn end and
# emits a `ha_cards` event. A contextvar so the tools (built once per profile)
# attribute cards to the turn that is actually running.
card_sink: contextvars.ContextVar[list[dict[str, Any]] | None] = contextvars.ContextVar(
    "ha_card_sink", default=None
)

# Domains that get a read-only state card in phase 1. Sensors render a value
# card (state + unit); binary_sensor/cover.garage an open/closed status; the
# actionable light/switch a current-state badge (controls are later phases).
_CARD_DOMAINS = frozenset(
    {"sensor", "binary_sensor", "cover", "light", "switch", "climate"}
)
# Attributes the phase-3 controls (sliders/colour/climate) read off the card-spec
# so the frontend can feature-gate them without a second HA round-trip (#477).
_CONTROL_ATTRS = (
    "supported_features",
    "brightness",
    "rgb_color",
    "color_mode",
    "supported_color_modes",
    "current_position",
    "temperature",
    "current_temperature",
    "target_temp_step",
    "min_temp",
    "max_temp",
    "hvac_modes",
)


def _emit_card(entity_id: str, name: str, state: Any, attrs: dict[str, Any]) -> None:
    """Append a card-spec for one entity to the turn's sink (#475, #477)."""
    sink = card_sink.get()
    if sink is None:
        return
    domain = entity_id.split(".", 1)[0]
    if domain not in _CARD_DOMAINS:
        return
    if any(c["entity_id"] == entity_id for c in sink):
        return
    spec = {
        "entity_id": entity_id,
        "name": name,
        "domain": domain,
        "device_class": attrs.get("device_class"),
        "state": None if state is None else str(state),
        "unit": attrs.get("unit_of_measurement"),
    }
    for key in _CONTROL_ATTRS:
        if attrs.get(key) is not None:
            spec[key] = attrs[key]
    sink.append(spec)


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


async def call_service_scoped(
    hass_url: str,
    hass_token: str,
    entity_id: str,
    service: str,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run a single `domain.service` on one entity and return its new state.

    The card-action path (#476): same domain/service allowlist as the
    `ha_call_service` tool — the names go into `/api/services/{domain}/{service}`
    so the regex blocks path traversal and `_BLOCKED_DOMAINS` keeps
    arbitrary-code domains unreachable. `service` is dotted (`light.toggle`); its
    domain must match the entity's so a card can only act on its own entity.
    """
    if not _ENTITY_RE.match(entity_id):
        return {"ok": False, "error": "invalid entity_id"}
    domain, _, action = service.partition(".")
    if not action or not _NAME_RE.match(domain) or not _NAME_RE.match(action):
        return {"ok": False, "error": "invalid service"}
    if domain in _BLOCKED_DOMAINS:
        return {"ok": False, "error": f"domain {domain} is not allowed"}
    if domain != entity_id.split(".", 1)[0]:
        return {"ok": False, "error": "service domain does not match entity"}
    headers = {
        "Authorization": f"Bearer {hass_token}",
        "Content-Type": "application/json",
    }
    payload: dict[str, Any] = {"entity_id": entity_id}
    if isinstance(data, dict):
        payload.update(data)
    url = hass_url.rstrip("/")
    async with aiohttp.ClientSession(timeout=_TIMEOUT) as client:
        async with client.post(
            f"{url}/api/services/{domain}/{action}", json=payload, headers=headers
        ) as resp:
            if resp.status >= 400:
                detail = (await resp.text())[:200]
                return {"ok": False, "error": f"HA {resp.status}: {detail}"}
        async with client.get(f"{url}/api/states/{entity_id}", headers=headers) as resp:
            if resp.status >= 400:
                return {"ok": True, "state": None}
            body = await resp.json()
    return {"ok": True, "state": body.get("state")}


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
        raw_attrs = body.get("attributes") or {}
        _emit_card(
            entity_id,
            raw_attrs.get("friendly_name") or entity_id,
            body.get("state"),
            raw_attrs,
        )
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
        want_class = str(args.get("device_class") or "").lower()
        name_q = str(args.get("name") or "").lower()
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as client:
            async with client.get(f"{url}/api/states", headers=headers) as resp:
                resp.raise_for_status()
                states = await resp.json()
        out = []
        truncated = False
        for s in states:
            eid = str(s.get("entity_id") or "")
            if domain and not eid.startswith(f"{domain}."):
                continue
            attrs = s.get("attributes") or {}
            if (
                want_class
                and str(attrs.get("device_class") or "").lower() != want_class
            ):
                continue
            name = attrs.get("friendly_name") or eid
            if name_q and name_q not in str(name).lower():
                continue
            _emit_card(eid, name, s.get("state"), attrs)
            out.append({"entity_id": eid, "state": s.get("state"), "name": name})
            # Cap to bound the prompt; the filters keep targeted queries well under it.
            if len(out) >= 200:
                truncated = True
                break
        if truncated:
            out.append(
                {"_note": "gekürzt — mit device_class, domain oder name eingrenzen"}
            )
        return json.dumps(out, ensure_ascii=False)

    async def _resolve_entity_id(ref: str) -> str:
        """Resolve a model-supplied reference to a real entity_id, "" on no match.

        A literal id is honoured ONLY if it actually exists — the model often
        guesses one from the name (e.g. `light.sofalicht` for "Sofalicht", whose
        real id is `light.dimmer_2_5`); a phantom id would otherwise sail through
        and return an empty history, reading as "never happened". So: an existing
        id wins; otherwise match the readable part against friendly_name
        (exact, then substring), preferring the guessed domain.
        """
        ref = ref.strip()
        if not ref:
            return ""
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as client:
            async with client.get(f"{url}/api/states", headers=headers) as resp:
                resp.raise_for_status()
                states = await resp.json()
        ids = {str(s.get("entity_id") or "") for s in states}
        if ref in ids:
            return ref
        # A guessed-but-missing id: search by its slug, biased to its domain.
        domain = ""
        term = ref
        if _ENTITY_RE.match(ref):
            domain, slug = ref.split(".", 1)
            term = slug.replace("_", " ")
        wanted = term.lower()
        best: tuple[int, str] | None = None
        for s in states:
            eid = str(s.get("entity_id") or "")
            name = str((s.get("attributes") or {}).get("friendly_name") or "").lower()
            in_dom = bool(domain) and eid.startswith(domain + ".")
            if name == wanted:
                pri = 0 if (not domain or in_dom) else 2
            elif wanted and wanted in name:
                pri = 1 if (not domain or in_dom) else 3
            else:
                continue
            if best is None or pri < best[0]:
                best = (pri, eid)
                if pri == 0:
                    break
        return best[1] if best else ""

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
                "Listet Entities mit Live-Zustand — für read-only Geräte, die NICHT"
                " in der Geräteliste des Prompts stehen (Sensoren etc.). Filter"
                " kombinierbar: device_class (z.B. 'temperature', 'humidity',"
                " 'power', 'energy', 'battery'), domain (z.B. 'sensor',"
                " 'binary_sensor') und name (Teilstring, z.B. 'Küche'). Beispiel:"
                " device_class='temperature' + name='Küche' für die Küchentemperatur."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "domain": {"type": "string"},
                    "device_class": {"type": "string"},
                    "name": {"type": "string"},
                },
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

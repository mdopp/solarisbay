"""HA tool tests (#369 state-history, #370 list/run scenes-scripts).

aiohttp is stubbed so each handler is exercised without a real HA, asserting
the request it builds and the shape it returns; guest scoping is checked
against profiles.build_engine_clients.
"""

from __future__ import annotations

import json

import pytest

from solaris_chat.engine.tools import ha as ha_mod
from solaris_chat.engine.tools.ha import build_ha_tools


class _Resp:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def json(self):
        return self._payload

    async def text(self):
        return ""

    def raise_for_status(self):
        if self.status >= 400:
            raise RuntimeError(self.status)


def _stub(monkeypatch, *, states=None, history=None, calls=None):
    """Stub aiohttp.ClientSession; record GET urls/params and POST bodies."""
    gets: list[tuple[str, dict]] = []

    class _Session:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def get(self, geturl, *, params=None, **k):
            gets.append((geturl, params or {}))
            if "/api/history/period/" in geturl:
                return _Resp(history)
            return _Resp(states)

        def post(self, posturl, *, json, **k):
            if calls is not None:
                calls.append((posturl, json))
            return _Resp({"ok": True})

    monkeypatch.setattr(ha_mod.aiohttp, "ClientSession", _Session)
    return gets


def _tool(name):
    tools = build_ha_tools("http://ha", "tok")
    return next(t for t in tools if t.name == name)


async def test_history_resolves_name_and_summarizes_transitions(monkeypatch):
    states = [
        {"entity_id": "light.kitchen", "attributes": {"friendly_name": "Küche"}},
    ]
    history = [
        [
            {"state": "off", "last_changed": "2026-06-01T08:00:00+00:00"},
            {"state": "on", "last_changed": "2026-06-01T09:00:00+00:00"},
            {"state": "on", "last_changed": "2026-06-01T09:30:00+00:00"},  # dup
            {"state": "off", "last_changed": "2026-06-01T10:00:00+00:00"},
        ]
    ]
    gets = _stub(monkeypatch, states=states, history=history)

    out = json.loads(await _tool("ha_state_history").handler({"entity": "Küche"}))

    assert out["entity_id"] == "light.kitchen"
    # name resolution hit /api/states, then the history period url
    assert any("/api/states" in u for u, _ in gets)
    hist = next((u, p) for u, p in gets if "/api/history/period/" in u)
    assert hist[1]["filter_entity_id"] == "light.kitchen"
    assert "end_time" in hist[1]
    # the duplicate "on" is collapsed; the "on" lasted one hour
    states_seq = [t["state"] for t in out["transitions"]]
    assert states_seq == ["off", "on", "off"]
    on = next(t for t in out["transitions"] if t["state"] == "on")
    assert on["duration_s"] == 3600


async def test_history_passes_through_entity_id_without_lookup(monkeypatch):
    gets = _stub(monkeypatch, states=[], history=[[]])
    await _tool("ha_state_history").handler({"entity": "light.kitchen"})
    # a literal entity_id must not trigger a /api/states resolution
    assert not any("/api/states" in u for u, _ in gets)


async def test_history_no_match(monkeypatch):
    _stub(monkeypatch, states=[], history=[[]])
    out = json.loads(await _tool("ha_state_history").handler({"entity": "Nope"}))
    assert "error" in out


async def test_list_entities_filters_by_device_class_and_name(monkeypatch):
    states = [
        {
            "entity_id": "sensor.kuche_temp",
            "state": "21.4",
            "attributes": {
                "friendly_name": "Küchensensor Air temperature",
                "device_class": "temperature",
            },
        },
        {
            "entity_id": "sensor.bad_temp",
            "state": "19.0",
            "attributes": {
                "friendly_name": "Bad Temperatur",
                "device_class": "temperature",
            },
        },
        {
            "entity_id": "sensor.wm_power",
            "state": "5",
            "attributes": {"friendly_name": "Waschmaschine", "device_class": "power"},
        },
        {
            "entity_id": "light.kuche",
            "state": "on",
            "attributes": {"friendly_name": "Küchenlicht"},
        },
    ]
    _stub(monkeypatch, states=states)
    # device_class narrows to the temperature sensors only
    out = json.loads(
        await _tool("ha_list_entities").handler({"device_class": "temperature"})
    )
    assert [e["entity_id"] for e in out] == ["sensor.kuche_temp", "sensor.bad_temp"]
    # device_class + name substring narrows to the kitchen one
    out = json.loads(
        await _tool("ha_list_entities").handler(
            {"device_class": "temperature", "name": "küche"}
        )
    )
    assert [e["entity_id"] for e in out] == ["sensor.kuche_temp"]
    assert out[0]["state"] == "21.4"


async def test_list_runnable_filters_to_domains(monkeypatch):
    states = [
        {"entity_id": "scene.movie", "attributes": {"friendly_name": "Kino"}},
        {"entity_id": "script.bedtime", "attributes": {}},
        {"entity_id": "automation.morning", "attributes": {}},
        {"entity_id": "light.kitchen", "attributes": {}},
    ]
    _stub(monkeypatch, states=states)
    out = json.loads(await _tool("ha_list_scenes_scripts").handler({}))
    ids = {e["entity_id"] for e in out}
    assert ids == {"scene.movie", "script.bedtime", "automation.morning"}


@pytest.mark.parametrize(
    "entity_id,service",
    [
        ("scene.movie", "turn_on"),
        ("script.bedtime", "turn_on"),
        ("automation.morning", "trigger"),
    ],
)
async def test_run_runnable_builds_service_call(monkeypatch, entity_id, service):
    calls: list[tuple[str, dict]] = []
    _stub(monkeypatch, states=[], calls=calls)
    domain = entity_id.split(".")[0]
    out = json.loads(await _tool("ha_run_scene_script").handler({"entity": entity_id}))
    assert out["success"] is True
    posturl, body = calls[0]
    assert posturl == f"http://ha/api/services/{domain}/{service}"
    assert body["entity_id"] == entity_id


async def test_run_runnable_rejects_non_runnable(monkeypatch):
    calls: list[tuple[str, dict]] = []
    _stub(monkeypatch, states=[], calls=calls)
    out = json.loads(
        await _tool("ha_run_scene_script").handler({"entity": "light.kitchen"})
    )
    assert "error" in out
    assert calls == []


@pytest.mark.parametrize(
    "service,expected",
    [("open", "open_cover"), ("close", "close_cover"), ("stop", "stop_cover")],
)
async def test_call_service_normalizes_cover_aliases(monkeypatch, service, expected):
    calls: list[tuple[str, dict]] = []
    _stub(monkeypatch, calls=calls)
    out = json.loads(
        await _tool("ha_call_service").handler(
            {"domain": "cover", "service": service, "entity_id": "cover.garage"}
        )
    )
    assert out["success"] is True
    assert out["service"] == f"cover.{expected}"
    posturl, _ = calls[0]
    assert posturl == f"http://ha/api/services/cover/{expected}"


@pytest.mark.parametrize(
    "domain,service",
    [("cover", "open_cover"), ("light", "turn_on"), ("climate", "set_temperature")],
)
async def test_call_service_passes_unmapped_through(monkeypatch, domain, service):
    calls: list[tuple[str, dict]] = []
    _stub(monkeypatch, calls=calls)
    out = json.loads(
        await _tool("ha_call_service").handler(
            {"domain": domain, "service": service, "entity_id": f"{domain}.x"}
        )
    )
    assert out["service"] == f"{domain}.{service}"
    posturl, _ = calls[0]
    assert posturl == f"http://ha/api/services/{domain}/{service}"


async def test_call_service_unknown_service_still_errors(monkeypatch):
    calls: list[tuple[str, dict]] = []

    class _Session:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def post(self, posturl, *, json, **k):
            calls.append((posturl, json))
            return _Resp({"message": "service not found"}, status=400)

    monkeypatch.setattr(ha_mod.aiohttp, "ClientSession", _Session)
    out = json.loads(
        await _tool("ha_call_service").handler(
            {"domain": "cover", "service": "levitate", "entity_id": "cover.garage"}
        )
    )
    assert "error" in out
    assert "400" in out["error"]
    # an unmapped service is forwarded as-is, not rewritten or dropped
    assert calls[0][0] == "http://ha/api/services/cover/levitate"


async def test_guest_toolset_excludes_run_tool():
    from solaris_chat.engine.profiles import build_engine_clients

    household, _, _, guest, _, _ = build_engine_clients(
        db_path=":memory:",
        ollama_url="http://o",
        fast_model="m",
        thorough_model="m",
        soul_path="/nonexistent/SOUL.md",
        hass_url="http://ha",
        hass_token="tok",
    )
    guest_names = set((await guest.list_toolsets())[0]["tools"])
    household_names = set((await household.list_toolsets())[0]["tools"])
    # the run-tool is device control beyond a guest's remit (#370)
    assert "ha_run_scene_script" not in guest_names
    assert "ha_run_scene_script" in household_names
    # read-only history is allowed for guests
    assert "ha_state_history" in guest_names


async def test_household_has_self_enrollment_tools():
    # First-run/owner self-enrolment (#396): an unknown speaker with zero
    # enrolments resolves to `household`, not `guest`, so the enrol tools must be
    # in the household toolbox too — not only the guest set — or "Setup starten"
    # can never bootstrap the first voice profile.
    from solaris_chat.engine.profiles import build_engine_clients

    household, deep, _, guest, _, _ = build_engine_clients(
        db_path=":memory:",
        ollama_url="http://o",
        fast_model="m",
        thorough_model="m",
        soul_path="/nonexistent/SOUL.md",
        hass_url="http://ha",
        hass_token="tok",
        gatekeeper_url="http://gk",
        gatekeeper_token="t",
    )
    household_names = set((await household.list_toolsets())[0]["tools"])
    deep_names = set((await deep.list_toolsets())[0]["tools"])
    guest_names = set((await guest.list_toolsets())[0]["tools"])
    for name in ("start_voice_enrollment", "register_pending_resident"):
        assert name in household_names
        assert name in deep_names
        # the guest path keeps them too (the heard-but-below-threshold flow)
        assert name in guest_names

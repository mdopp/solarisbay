"""Chat-proxy routing across the two Hermes gateways (#293).

The household gateway (`hermes`, :8642) serves every resident session; the admin
gateway (`hermes_admin`, :8643) serves only the admin-gated servicebay-maintenance
path. These tests pin the routing contract: a household turn lands on household, a
maintenance turn lands on admin, a non-admin never reaches admin, and the #278
dropdown's admin persona selects the admin gateway — all server-enforced.
"""

from __future__ import annotations

from solilos_chat import personalities
from solilos_chat.server import build_app

from .test_server import _FakeHermes

ADMIN_HDRS = {"Remote-User": "mdopp", "Remote-Groups": "admins"}
RESIDENT_HDRS = {"Remote-User": "cdopp", "Remote-Groups": "family"}


def _app(household, admin):
    return build_app(
        hermes=household,
        hermes_admin=admin,
        remote_user_header="Remote-User",
        default_uid="household",
    )


async def test_household_chat_routes_to_household_gateway(aiohttp_client):
    household, admin = _FakeHermes(), _FakeHermes()
    client = await aiohttp_client(_app(household, admin))

    resp = await client.post(
        "/api/chat", json={"input": "wie spät ist es?"}, headers=RESIDENT_HDRS
    )
    assert resp.status == 200
    # A first-turn resident chat creates + turns on household; admin untouched.
    assert household.created == ["cdopp"]
    assert household.turns and household.turns[0][0] == "sess-1"
    assert admin.created == []
    assert admin.turns == []


async def test_resident_followup_turn_routes_to_household(aiohttp_client):
    # A resident reusing an existing (household) session id keeps every follow-up
    # turn on the household gateway — the pinned "Zuhause" chat (#237) is a normal
    # resident session and never leaks onto admin.
    household, admin = _FakeHermes(), _FakeHermes()
    client = await aiohttp_client(_app(household, admin))

    resp = await client.post(
        "/api/chat",
        json={"input": "mach das licht an", "session_id": "sess-9"},
        headers=RESIDENT_HDRS,
    )
    assert resp.status == 200
    assert household.turns and household.turns[0][0] == "sess-9"
    assert admin.turns == []


async def test_maintenance_session_create_and_turns_route_to_admin(
    aiohttp_client, monkeypatch
):
    from solilos_chat import server as server_mod

    async def fake_soul(url, token):
        return "# Admin Soul"

    monkeypatch.setattr(server_mod, "_agent_get_soul", fake_soul)
    household, admin = _FakeHermes(), _FakeHermes()
    client = await aiohttp_client(_app(household, admin))

    # Admin opens the servicebay-maintenance session: created on the ADMIN
    # gateway with the live soul + maintenance marker, household untouched.
    resp = await client.post(
        "/api/sessions?persona=servicebay-maintenance", headers=ADMIN_HDRS
    )
    body = await resp.json()
    assert resp.status == 200
    sid = body["session_id"]
    assert admin.created == ["mdopp"]
    assert admin.maintenance == [True]
    assert household.created == []

    # A follow-up turn carrying that session id routes back to the SAME (admin)
    # gateway — Hermes session state is per-gateway, so the session must stay put.
    resp = await client.post(
        "/api/chat", json={"input": "status", "session_id": sid}, headers=ADMIN_HDRS
    )
    assert resp.status == 200
    assert admin.turns and admin.turns[0][0] == sid
    assert household.turns == []


async def test_non_admin_maintenance_create_forbidden_no_admin_gateway(
    aiohttp_client, monkeypatch
):
    from solilos_chat import server as server_mod

    called = []

    async def fake_soul(url, token):
        called.append(1)
        return "# Admin Soul"

    monkeypatch.setattr(server_mod, "_agent_get_soul", fake_soul)
    household, admin = _FakeHermes(), _FakeHermes()
    client = await aiohttp_client(_app(household, admin))

    resp = await client.post(
        "/api/sessions?persona=servicebay-maintenance", headers=RESIDENT_HDRS
    )
    assert resp.status == 403
    # Neither gateway created a session; the soul was never even fetched.
    assert admin.created == [] and household.created == []
    assert called == []


async def test_non_admin_admin_persona_turn_never_reaches_admin(aiohttp_client):
    # A non-admin sending the admin/maintenance persona on a chat turn is routed
    # to household, never admin — the Remote-Groups gate holds at the router.
    household, admin = _FakeHermes(), _FakeHermes()
    client = await aiohttp_client(_app(household, admin))

    resp = await client.post(
        "/api/chat",
        json={"input": "status", "personality": personalities.MAINTENANCE_ID},
        headers=RESIDENT_HDRS,
    )
    assert resp.status == 200
    assert household.turns and admin.turns == []
    assert admin.created == []


async def test_non_admin_with_known_admin_session_id_stays_household(aiohttp_client):
    # Even presenting a session id that lives on the admin gateway, a non-admin
    # is routed to household — knowing an id can't escalate the gateway choice.
    household, admin = _FakeHermes(), _FakeHermes()
    client = await aiohttp_client(_app(household, admin))

    resp = await client.post(
        "/api/chat",
        json={"input": "status", "session_id": "maint-1"},
        headers=RESIDENT_HDRS,
    )
    assert resp.status == 200
    assert household.turns and household.turns[0][0] == "maint-1"
    assert admin.turns == []


async def test_admin_dropdown_persona_routes_new_chat_to_admin(aiohttp_client):
    # The #278 dropdown's "Admin" option sends personality=servicebay-maintenance
    # on a fresh chat; an admin caller routes that create + turn to the admin
    # gateway (the dropdown selects the profile/gateway, server re-checks the gate).
    household, admin = _FakeHermes(), _FakeHermes()
    client = await aiohttp_client(_app(household, admin))

    resp = await client.post(
        "/api/chat",
        json={"input": "deploy status", "personality": personalities.MAINTENANCE_ID},
        headers=ADMIN_HDRS,
    )
    assert resp.status == 200
    assert admin.created == ["mdopp"]
    assert admin.turns and admin.turns[0][0] == "sess-1"
    assert household.created == [] and household.turns == []


async def test_admin_household_persona_still_routes_to_household(aiohttp_client):
    # An admin choosing a normal household persona (e.g. technical) is a resident
    # chat — it must stay on the household gateway, not leak onto admin.
    household, admin = _FakeHermes(), _FakeHermes()
    client = await aiohttp_client(_app(household, admin))

    resp = await client.post(
        "/api/chat",
        json={"input": "erklär mir das", "personality": "technical"},
        headers=ADMIN_HDRS,
    )
    assert resp.status == 200
    assert household.turns and admin.turns == []


async def test_stream_maintenance_session_routes_to_admin(aiohttp_client, monkeypatch):
    from solilos_chat import server as server_mod

    async def fake_soul(url, token):
        return "# Admin Soul"

    monkeypatch.setattr(server_mod, "_agent_get_soul", fake_soul)
    household = _FakeHermes()
    admin = _FakeHermes(events=[{"type": "assistant.delta", "data": {"delta": "ok"}}])
    client = await aiohttp_client(_app(household, admin))

    resp = await client.post(
        "/api/sessions?persona=servicebay-maintenance", headers=ADMIN_HDRS
    )
    sid = (await resp.json())["session_id"]

    resp = await client.post(
        "/api/chat/stream",
        json={"input": "logs", "session_id": sid},
        headers=ADMIN_HDRS,
    )
    assert resp.status == 200
    await resp.text()
    assert admin.turns and admin.turns[0][0] == sid
    assert household.turns == []


async def test_falls_back_to_household_when_no_admin_gateway(aiohttp_client):
    # No admin gateway configured (single-instance/offline): admin routing is a
    # no-op — everything stays on household and nothing breaks.
    household = _FakeHermes()
    app = build_app(
        hermes=household,
        remote_user_header="Remote-User",
        default_uid="household",
    )
    client = await aiohttp_client(app)

    resp = await client.post(
        "/api/chat",
        json={"input": "status", "personality": personalities.MAINTENANCE_ID},
        headers=ADMIN_HDRS,
    )
    assert resp.status == 200
    assert household.turns

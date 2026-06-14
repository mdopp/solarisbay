"""Chat-proxy routing across the two Hermes gateways (#293).

The household gateway (`hermes`, :8642) serves every resident session; the admin
gateway (`hermes_admin`, :8643) serves only the admin-gated servicebay-maintenance
path. These tests pin the routing contract: a household turn lands on household, a
maintenance turn lands on admin, a non-admin never reaches admin, and the #278
dropdown's admin persona selects the admin gateway — all server-enforced.
"""

from __future__ import annotations

import sqlite3

from solaris_chat import personalities, settings_store, topics_store
from solaris_chat.server import build_app

from .test_server import _FakeHermes

ADMIN_HDRS = {"Remote-User": "mdopp", "Remote-Groups": "admins"}
RESIDENT_HDRS = {"Remote-User": "cdopp", "Remote-Groups": "family"}

# Minimal session_topics schema (migration 0005) so create can persist a primary
# topic and follow-up routing can read it back.
_SCHEMA = """
CREATE TABLE topics (
  slug TEXT PRIMARY KEY,
  display_name TEXT NOT NULL,
  scope TEXT NOT NULL DEFAULT 'resident',
  owner_uid TEXT
);
CREATE TABLE session_topics (
  session_id TEXT NOT NULL,
  topic_slug TEXT NOT NULL,
  role TEXT NOT NULL DEFAULT 'secondary',
  owner_uid TEXT NOT NULL,
  PRIMARY KEY (session_id, topic_slug)
);
CREATE UNIQUE INDEX session_topics_one_primary_idx
  ON session_topics (session_id) WHERE role = 'primary';
"""


def _db(tmp_path) -> str:
    path = str(tmp_path / "solaris.db")
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA)
    conn.execute(
        "INSERT INTO topics (slug, display_name, scope) VALUES (?, ?, ?)",
        ("household", "Zuhause", "system"),
    )
    conn.commit()
    conn.close()
    return path


def _app(household, admin):
    return build_app(
        hermes=household,
        hermes_admin=admin,
        remote_user_header="Remote-User",
        default_uid="household",
    )


def _deep_app(household, deep, tmp_path, *, pref="thorough"):
    db = _db(tmp_path)
    settings_store.set_other_model_pref(db, pref)
    return build_app(
        hermes=household,
        hermes_deep=deep,
        remote_user_header="Remote-User",
        default_uid="household",
        solaris_db_path=db,
        attachments_dir=str(tmp_path / "att"),
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


async def test_maintenance_session_create_and_turns_route_to_admin(aiohttp_client):
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
    aiohttp_client,
):
    household, admin = _FakeHermes(), _FakeHermes()
    client = await aiohttp_client(_app(household, admin))

    resp = await client.post(
        "/api/sessions?persona=servicebay-maintenance", headers=RESIDENT_HDRS
    )
    assert resp.status == 403
    # Neither gateway created a session.
    assert admin.created == [] and household.created == []


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


async def test_stream_maintenance_session_routes_to_admin(aiohttp_client):
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


# ---- everyday-chat model preference routing (#332-followup) ----


async def test_household_topic_chat_routes_to_household_even_when_thorough(
    aiohttp_client, tmp_path
):
    # The pinned "Zuhause" chat (primary topic = household) is ALWAYS the fast
    # e2b household gateway, even though the everyday-chat preference is thorough.
    household, deep = _FakeHermes(), _FakeHermes()
    client = await aiohttp_client(_deep_app(household, deep, tmp_path, pref="thorough"))

    resp = await client.post(
        "/api/chat",
        json={"input": "mach das licht an", "topic": "household"},
        headers=RESIDENT_HDRS,
    )
    assert resp.status == 200
    assert household.turns and household.turns[0][0] == "sess-1"
    assert deep.turns == []
    # Household turns are fast-only regardless of any selector.
    assert household.efforts == ["none"]


async def test_household_followup_reads_persisted_primary_topic(
    aiohttp_client, tmp_path
):
    # A follow-up turn (different in-memory app state) routes to household by the
    # persisted primary topic, not just the first-turn topic hint.
    household, deep = _FakeHermes(), _FakeHermes()
    db = _db(tmp_path)
    settings_store.set_other_model_pref(db, "thorough")
    topics_store.set_primary(db, "sess-42", "household", "cdopp")
    app = build_app(
        hermes=household,
        hermes_deep=deep,
        remote_user_header="Remote-User",
        default_uid="household",
        solaris_db_path=db,
        attachments_dir=str(tmp_path / "att"),
    )
    client = await aiohttp_client(app)

    resp = await client.post(
        "/api/chat",
        json={"input": "und im flur?", "session_id": "sess-42"},
        headers=RESIDENT_HDRS,
    )
    assert resp.status == 200
    assert household.turns and household.turns[0][0] == "sess-42"
    assert deep.turns == []


async def test_other_chat_thorough_routes_to_deep(aiohttp_client, tmp_path):
    # A normal (non-household) chat with the thorough preference routes to the
    # solaris-deep (12b) gateway and keeps the resident's reasoning selector.
    household, deep = _FakeHermes(), _FakeHermes()
    client = await aiohttp_client(_deep_app(household, deep, tmp_path, pref="thorough"))

    resp = await client.post(
        "/api/chat",
        json={"input": "erklär mir das", "reasoning": "high"},
        headers=RESIDENT_HDRS,
    )
    assert resp.status == 200
    assert deep.turns and deep.turns[0][0] == "sess-1"
    assert household.turns == []
    assert deep.efforts == ["high"]


async def test_other_chat_fast_routes_to_household(aiohttp_client, tmp_path):
    # The same normal chat with the fast preference stays on the e2b household
    # gateway.
    household, deep = _FakeHermes(), _FakeHermes()
    client = await aiohttp_client(_deep_app(household, deep, tmp_path, pref="fast"))

    resp = await client.post(
        "/api/chat", json={"input": "erklär mir das"}, headers=RESIDENT_HDRS
    )
    assert resp.status == 200
    assert household.turns and deep.turns == []


async def test_model_put_toggles_routing(aiohttp_client, tmp_path):
    # The admin Model setting is a live routing toggle: after switching to fast,
    # a fresh normal chat routes to household instead of deep — no restart.
    household, deep = _FakeHermes(), _FakeHermes()
    client = await aiohttp_client(_deep_app(household, deep, tmp_path, pref="thorough"))

    resp = await client.put("/api/model", json={"value": "fast"}, headers=ADMIN_HDRS)
    assert resp.status == 200
    assert (await resp.json())["current"] == "fast"

    resp = await client.post(
        "/api/chat", json={"input": "noch eine frage"}, headers=RESIDENT_HDRS
    )
    assert resp.status == 200
    assert household.turns and deep.turns == []


async def test_model_get_returns_options_and_current(aiohttp_client, tmp_path):
    household, deep = _FakeHermes(), _FakeHermes()
    client = await aiohttp_client(_deep_app(household, deep, tmp_path, pref="fast"))

    resp = await client.get("/api/model", headers=ADMIN_HDRS)
    assert resp.status == 200
    body = await resp.json()
    assert body["current"] == "fast"
    assert [o["value"] for o in body["options"]] == ["fast", "thorough"]


async def test_model_put_rejects_unknown_value(aiohttp_client, tmp_path):
    household, deep = _FakeHermes(), _FakeHermes()
    client = await aiohttp_client(_deep_app(household, deep, tmp_path))

    resp = await client.put("/api/model", json={"value": "12b"}, headers=ADMIN_HDRS)
    assert resp.status == 400


async def test_model_get_forbidden_for_non_admin(aiohttp_client, tmp_path):
    household, deep = _FakeHermes(), _FakeHermes()
    client = await aiohttp_client(_deep_app(household, deep, tmp_path))

    resp = await client.get("/api/model", headers=RESIDENT_HDRS)
    assert resp.status == 403

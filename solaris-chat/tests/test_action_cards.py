"""Action-card callback → handler mapping + confirm-gate (Wartung P2a, #787).

Covers the registry dispatch (an action_id maps to its server-side handler) and
the confirm-gate on a destructive action: a bare tap is 403 (confirm_required),
and the same tap with confirmed=true runs the handler. The frontend
renderActionCard and the SessionBus mirror are box-verified.
"""

from __future__ import annotations

from solaris_chat.engine import action_cards
from solaris_chat.server import build_app


class _FakeEngine:
    async def dispatch_tool(self, name, arguments):  # pragma: no cover - unused
        return "{}"


def _app(tmp_path):
    return build_app(
        engine=_FakeEngine(),
        remote_user_header="Remote-User",
        default_uid="household",
        solaris_db_path=str(tmp_path / "solaris.db"),
        notes_dir=str(tmp_path),
    )


async def test_callback_dispatches_to_registered_handler(aiohttp_client, tmp_path):
    ran: list[dict] = []

    async def handler(body):
        ran.append(body)
        return {"ok": True, "detail": "did-it"}

    action_cards.register("u87-safe", handler)
    client = await aiohttp_client(_app(tmp_path))
    r = await client.post("/api/action-callback", json={"action_id": "u87-safe"})
    assert r.status == 200
    assert (await r.json())["detail"] == "did-it"
    assert len(ran) == 1


async def test_callback_unknown_action_is_404(aiohttp_client, tmp_path):
    client = await aiohttp_client(_app(tmp_path))
    r = await client.post("/api/action-callback", json={"action_id": "nope"})
    assert r.status == 404
    assert (await r.json())["reason"] == "unknown_action"


async def test_destructive_action_is_confirm_gated(aiohttp_client, tmp_path):
    ran: list[dict] = []

    async def handler(body):
        ran.append(body)
        return {"ok": True}

    action_cards.register("u87-danger", handler, destructive=True)
    client = await aiohttp_client(_app(tmp_path))

    # A bare tap on a destructive action is refused — the handler must not run.
    r = await client.post("/api/action-callback", json={"action_id": "u87-danger"})
    assert r.status == 403
    assert (await r.json())["reason"] == "confirm_required"
    assert ran == []

    # The re-sent confirmed tap runs it.
    r = await client.post(
        "/api/action-callback",
        json={"action_id": "u87-danger", "confirmed": True},
    )
    assert r.status == 200
    assert len(ran) == 1


async def test_admin_action_forbidden_for_non_admin(aiohttp_client, tmp_path):
    ran: list[dict] = []

    async def handler(body):
        ran.append(body)
        return {"ok": True}

    action_cards.register("u796-admin", handler, admin=True)
    client = await aiohttp_client(_app(tmp_path))

    # A non-admin caller (no admins group) is refused before the handler runs.
    r = await client.post("/api/action-callback", json={"action_id": "u796-admin"})
    assert r.status == 403
    assert (await r.json())["reason"] == "forbidden"
    assert ran == []

    # confirmed=true cannot buy past the admin gate.
    r = await client.post(
        "/api/action-callback",
        json={"action_id": "u796-admin", "confirmed": True},
    )
    assert r.status == 403
    assert (await r.json())["reason"] == "forbidden"
    assert ran == []


async def test_admin_action_runs_for_admin(aiohttp_client, tmp_path):
    ran: list[dict] = []

    async def handler(body):
        ran.append(body)
        return {"ok": True, "detail": "done"}

    action_cards.register("u796-admin-ok", handler, admin=True)
    client = await aiohttp_client(_app(tmp_path))

    r = await client.post(
        "/api/action-callback",
        json={"action_id": "u796-admin-ok"},
        headers={"Remote-Groups": "admins"},
    )
    assert r.status == 200
    assert (await r.json())["detail"] == "done"
    assert len(ran) == 1


async def test_non_admin_action_runs_for_resident(aiohttp_client, tmp_path):
    # A non-admin action (the default, e.g. ping) still fires for any resident.
    client = await aiohttp_client(_app(tmp_path))
    r = await client.post("/api/action-callback", json={"action_id": "ping"})
    assert r.status == 200
    assert (await r.json())["detail"] == "pong"


async def test_callback_requires_action_id(aiohttp_client, tmp_path):
    client = await aiohttp_client(_app(tmp_path))
    r = await client.post("/api/action-callback", json={})
    assert r.status == 400
    assert (await r.json())["reason"] == "no_action_id"


def test_registry_get_returns_none_for_unknown():
    assert action_cards.get("definitely-not-registered") is None

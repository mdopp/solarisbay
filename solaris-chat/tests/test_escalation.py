"""One-shot elevated delete/exec escalation (Wartung P2c, #789).

The Wartung ambient SB-MCP token is read+lifecycle+mutate — a delete/exec call
is refused by SB-MCP with a "Token scope 'destroy'|'exec' required" message. This
covers the P2c routing: such a refusal is NOT surfaced raw and NOT run — it files
a ONE-SHOT owner-approval via `request_token` (bound to exactly that op) and
injects an [Approve]/[Deny] card; the model gets a "pending_approval" result. On
[Approve] the handler polls `poll_token_request`, runs the bound op ONCE over a
FRESH connection carrying only that one-shot token, and the ambient token is
never elevated. [Deny] runs nothing. The request_token/poll_token_request wire
calls are asserted (field names come from SB PR #2266, not guessed).
"""

from __future__ import annotations

import json

from solaris_chat.engine import action_cards, escalation
from solaris_chat.engine.tools.mcp_tools import (
    McpToolbox,
    _op_service,
    _scope_refusal,
)
from solaris_chat.server import build_app

_DESTROY_REFUSAL = (
    "Token scope 'destroy' required for delete_service; "
    "this token has [read,lifecycle,mutate]"
)
_EXEC_REFUSAL = (
    "Token scope 'exec' required for exec_command; "
    "this token has [read,lifecycle,mutate]"
)


class _FakeMcp(McpToolbox):
    """McpToolbox with the MCP wire stubbed by canned per-tool responses.

    `wire[tool_name]` is either a string (fixed result) or a callable
    (arguments, headers) -> str. Every call is recorded with the headers it
    carried, so a test can prove the one-shot token rides ONLY the bound op and
    never the ambient toolbox."""

    def __init__(self, wire, ambient_token="sb_ambient_rlm"):
        super().__init__("http://mcp", "/tmp/nonexistent-token")
        self._names = ["delete_service", "exec_command", "list_services"]
        self._wire = wire
        self._ambient = ambient_token
        self.calls: list[tuple[str, dict, dict]] = []

    def _headers(self):
        return {"Authorization": f"Bearer {self._ambient}"}

    async def _call_tool_with(self, name, arguments, headers):
        self.calls.append((name, dict(arguments), dict(headers)))
        entry = self._wire.get(name)
        if callable(entry):
            return entry(arguments, headers)
        return entry if entry is not None else json.dumps({"ok": True})


# ---- parsing helpers -------------------------------------------------------


def test_scope_refusal_parses_destroy_and_exec():
    assert _scope_refusal(_DESTROY_REFUSAL) == ("destroy", "delete_service")
    assert _scope_refusal(_EXEC_REFUSAL) == ("exec", "exec_command")


def test_scope_refusal_ignores_non_refusal():
    assert _scope_refusal('{"ok": true, "id": "svc"}') is None
    # A mutate-tier refusal never reaches the ambient token (it HAS mutate), but
    # even if it did it is not an escalation target.
    assert _scope_refusal("Token scope 'mutate' required for deploy_service") is None


def test_op_service_matches_sb_anchor():
    assert _op_service({"name": "demo-svc"}) == "demo-svc"
    assert _op_service({"service": "media"}) == "media"
    assert _op_service({"name": "../etc"}) is None
    assert _op_service({}) is None


# ---- routing: refused destroy/exec → approval card, not immediate run -------


async def test_destroy_refusal_routes_to_one_shot_request_not_run():
    injected: list = []

    async def sink(op, request_id, approval_id):
        injected.append((op, request_id, approval_id))

    box = _FakeMcp(
        wire={
            "delete_service": _DESTROY_REFUSAL,
            "request_token": json.dumps(
                {"ok": True, "id": "req-1", "status": "pending", "approvalId": "ap-9"}
            ),
        },
    )
    box._on_escalation = sink

    out = await box.dispatch("delete_service", {"name": "demo-svc"})
    parsed = json.loads(out)
    assert parsed["status"] == "pending_approval"
    assert parsed["request_id"] == "req-1"

    # request_token was filed for exactly this op, one elevated scope, one-shot.
    rt = [c for c in box.calls if c[0] == "request_token"]
    assert len(rt) == 1
    args = rt[0][1]
    assert args["scopes"] == ["destroy"]
    assert args["one_shot_op"] == {"tool_name": "delete_service", "service": "demo-svc"}
    assert args["ttl_seconds"] == 600
    assert args["reason"]

    # request_token rode the AMBIENT token (it needs only read); nothing destructive ran.
    assert rt[0][2]["Authorization"] == "Bearer sb_ambient_rlm"
    assert not any(
        c[0] == "delete_service" and "req" not in c[0] for c in box.calls[1:]
    )
    # The card was injected with the op + request id.
    assert len(injected) == 1
    assert injected[0][0]["tool_name"] == "delete_service"
    assert injected[0][1] == "req-1"
    assert injected[0][2] == "ap-9"


async def test_exec_refusal_binds_op_to_no_service():
    injected: list = []

    async def sink(op, request_id, approval_id):
        injected.append(op)

    box = _FakeMcp(
        wire={
            "exec_command": _EXEC_REFUSAL,
            "request_token": json.dumps({"ok": True, "id": "req-2"}),
        },
    )
    box._on_escalation = sink
    await box.dispatch("exec_command", {"command": "ls /"})
    rt = [c for c in box.calls if c[0] == "request_token"][0]
    assert rt[1]["scopes"] == ["exec"]
    assert rt[1]["one_shot_op"] == {"tool_name": "exec_command"}  # no service anchor


async def test_no_escalation_sink_surfaces_refusal_unchanged():
    # Without a sink wired the refusal is returned raw (nothing escalates).
    box = _FakeMcp(wire={"delete_service": _DESTROY_REFUSAL})
    out = await box.dispatch("delete_service", {"name": "demo-svc"})
    assert out == _DESTROY_REFUSAL
    assert not any(c[0] == "request_token" for c in box.calls)


async def test_non_refusal_result_passes_through():
    box = _FakeMcp(wire={"list_services": json.dumps({"services": []})})
    box._on_escalation = lambda *a: None  # never called
    out = await box.dispatch("list_services", {})
    assert json.loads(out) == {"services": []}
    assert not any(c[0] == "request_token" for c in box.calls)


# ---- Approve: run once with the one-shot token, ambient stays unelevated ----


async def test_run_one_shot_polls_then_runs_bound_op_with_one_shot_token():
    ran: list = []

    def delete_service(args, headers):
        ran.append(headers["Authorization"])
        return json.dumps({"ok": True, "trashed": args["name"]})

    box = _FakeMcp(
        wire={
            "poll_token_request": json.dumps(
                {
                    "id": "req-1",
                    "status": "approved",
                    "token": "sb_oneshot_destroy",
                    "grantedScopes": ["destroy"],
                    "collected": True,
                }
            ),
            "delete_service": delete_service,
        },
    )
    ok, detail = await box.run_one_shot("delete_service", {"name": "demo-svc"}, "req-1")
    assert ok is True
    assert "demo-svc" in detail

    # poll_token_request was called with the request id.
    poll = [c for c in box.calls if c[0] == "poll_token_request"][0]
    assert poll[1] == {"id": "req-1"}

    # The bound op ran EXACTLY once, carrying the ONE-SHOT token — not the ambient.
    assert ran == ["Bearer sb_oneshot_destroy"]

    # The ambient toolbox never cached the one-shot token: a later ambient call
    # still uses read+lifecycle+mutate, no standing destroy/exec.
    assert box._session_token == ""
    assert box._headers()["Authorization"] == "Bearer sb_ambient_rlm"


async def test_run_one_shot_no_token_when_still_pending():
    box = _FakeMcp(
        wire={
            "poll_token_request": json.dumps(
                {"id": "req-1", "status": "pending", "token": None}
            )
        },
    )
    ok, detail = await box.run_one_shot("delete_service", {"name": "x"}, "req-1")
    assert ok is False
    assert "pending" in detail
    # No destructive op ran.
    assert not any(c[0] == "delete_service" for c in box.calls)


async def test_run_one_shot_denied_runs_nothing():
    box = _FakeMcp(
        wire={
            "poll_token_request": json.dumps(
                {"id": "req-1", "status": "denied", "token": None}
            )
        },
    )
    ok, _ = await box.run_one_shot("delete_service", {"name": "x"}, "req-1")
    assert ok is False
    assert not any(c[0] == "delete_service" for c in box.calls)


# ---- card shape ------------------------------------------------------------


def test_card_names_exact_op_and_carries_request_id():
    c = escalation.card(
        {
            "tool_name": "delete_service",
            "service": "demo-svc",
            "arguments": {"name": "demo-svc"},
        },
        "req-7",
    )
    assert c["kind"] == "action"
    assert "delete_service" in c["body"] and "demo-svc" in c["body"]
    approve, deny = c["buttons"]
    assert approve["action_id"] == escalation.RUN_ACTION
    assert approve["destructive"] is True
    assert approve["params"]["request_id"] == "req-7"
    assert approve["params"]["tool_name"] == "delete_service"
    assert deny["action_id"] == escalation.DENY_ACTION
    assert "destructive" not in deny


# ---- handler registration + gate -------------------------------------------


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


def test_run_op_registered_admin_destructive(tmp_path):
    _app(tmp_path)
    run = action_cards.get(escalation.RUN_ACTION)
    deny = action_cards.get(escalation.DENY_ACTION)
    assert run is not None and deny is not None
    # Approve runs the destructive op → admin + destructive (confirm-gated).
    assert run.admin is True and run.destructive is True
    # Deny runs nothing → admin-only, no confirm.
    assert deny.admin is True and deny.destructive is False


async def test_run_op_forbidden_for_non_admin(aiohttp_client, tmp_path):
    client = await aiohttp_client(_app(tmp_path))
    r = await client.post(
        "/api/action-callback",
        json={
            "action_id": escalation.RUN_ACTION,
            "params": {"request_id": "r", "tool_name": "delete_service"},
        },
    )
    assert r.status == 403
    assert (await r.json())["reason"] == "forbidden"


async def test_run_op_confirm_gated_for_admin(aiohttp_client, tmp_path):
    client = await aiohttp_client(_app(tmp_path))
    # A bare admin tap on Approve is confirm-gated — the op must not run.
    r = await client.post(
        "/api/action-callback",
        headers={"Remote-Groups": "admins"},
        json={
            "action_id": escalation.RUN_ACTION,
            "params": {"request_id": "r", "tool_name": "delete_service"},
        },
    )
    assert r.status == 403
    assert (await r.json())["reason"] == "confirm_required"


async def test_deny_op_admin_runs_nothing(aiohttp_client, tmp_path):
    client = await aiohttp_client(_app(tmp_path))
    r = await client.post(
        "/api/action-callback",
        headers={"Remote-Groups": "admins", "Remote-User": "michael"},
        json={
            "action_id": escalation.DENY_ACTION,
            "params": {"request_id": "r", "tool_name": "delete_service"},
        },
    )
    assert r.status == 200
    body = await r.json()
    assert body["ok"] is True and body["denied"] is True

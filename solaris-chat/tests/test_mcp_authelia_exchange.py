"""Admin SB-MCP token comes from the acting admin's Authelia session (#794).

No standing token-minting credential lives in the pod: on a 401 from the MCP
endpoint the toolbox exchanges the acting admin's forward-auth identity
(Remote-User/Remote-Groups, pinned in `current_admin_identity`) for a
short-lived scoped token via `token-from-authelia-session`, then retries once.
A 401 surfaces from the streamable-http client wrapped in a task-group
ExceptionGroup (anyio), so the detection must unwrap it. A non-admin turn
carries no identity, so nothing is exchanged and no token is ever minted.
"""

from __future__ import annotations

import contextlib

import pytest

from solaris_chat.engine.client import current_admin_identity
from solaris_chat.engine.tools.mcp_tools import McpToolbox, _is_401

SB_API = "http://sb.test"


class _Resp:
    def __init__(self, status_code: int):
        self.status_code = status_code


class _Http401(Exception):
    """Stand-in for httpx.HTTPStatusError — duck-typed on `.response`."""

    def __init__(self):
        super().__init__("401 Unauthorized")
        self.response = _Resp(401)


def _wrapped_401() -> BaseException:
    return BaseExceptionGroup("unhandled errors in a TaskGroup", [_Http401()])


class _FakeExchange:
    """A fake aiohttp POST endpoint capturing the headers the exchange sent and
    returning a minted token (or a status the caller must treat as failure)."""

    def __init__(self, *, status=200, token="sb_new_session_token"):
        self.status = status
        self._token = token
        self.calls: list[dict] = []

    def install(self, monkeypatch):
        exchange = self

        class _Ctx:
            def __init__(self, headers):
                self.status = exchange.status
                exchange.calls.append({"headers": dict(headers or {})})

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def json(self):
                return {"token": exchange._token, "scopes": ["read"], "expiresAt": "z"}

        class _Session:
            def __init__(self, *a, **k):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            def post(self, url, *, headers=None, **k):
                return _Ctx(headers)

        monkeypatch.setattr(
            "solaris_chat.engine.tools.mcp_tools.aiohttp.ClientSession", _Session
        )


@pytest.fixture(autouse=True)
def _clear_identity():
    tok = current_admin_identity.set(("", ""))
    yield
    with contextlib.suppress(ValueError):
        current_admin_identity.reset(tok)


def test_is_401_unwraps_exceptiongroup():
    assert _is_401(_wrapped_401()) is True
    assert _is_401(ValueError("nope")) is False


@pytest.mark.asyncio
async def test_401_triggers_exchange_and_retry(monkeypatch):
    """A 401 on dispatch → exchange the admin session → retry succeeds, and the
    retry carries the freshly minted token."""
    current_admin_identity.set(("alice", "users,admins"))
    exchange = _FakeExchange(token="sb_fresh_1")
    exchange.install(monkeypatch)

    box = McpToolbox("http://mcp.test/mcp", "/nonexistent-token-file", SB_API)
    box._names = ["restart_service"]

    calls: list[str] = []

    async def fake_call(name, arguments):
        calls.append(box._headers().get("Authorization", ""))
        if len(calls) == 1:
            raise _wrapped_401()
        return '{"ok": true}'

    monkeypatch.setattr(box, "_call_tool", fake_call)

    out = await box.dispatch("restart_service", {})
    assert out == '{"ok": true}'
    # Exactly one exchange, forwarding the admin's forward-auth identity.
    assert len(exchange.calls) == 1
    sent = exchange.calls[0]["headers"]
    assert sent["Remote-User"] == "alice"
    assert sent["Remote-Groups"] == "users,admins"
    assert "Authorization" not in sent  # never a client Bearer (endpoint 403s it)
    # The retry used the freshly exchanged token, not the (absent) file token.
    assert calls[0] == ""  # first attempt: no token file → empty header
    assert calls[1] == "Bearer sb_fresh_1"


@pytest.mark.asyncio
async def test_non_admin_turn_mints_no_token(monkeypatch):
    """No admin identity in context → the exchange is never attempted and no
    token is minted (a household/guest turn can't self-elevate)."""
    # current_admin_identity stays ("", "") from the fixture.
    exchange = _FakeExchange()
    exchange.install(monkeypatch)

    box = McpToolbox("http://mcp.test/mcp", "/nonexistent-token-file", SB_API)
    assert await box._exchange_token() is False
    assert exchange.calls == []
    assert box._session_token == ""


@pytest.mark.asyncio
async def test_no_sb_api_url_skips_exchange(monkeypatch):
    """Empty SB API base ⇒ no runtime exchange even for an admin (a rotation
    then needs a redeploy of the deploy-time token file)."""
    current_admin_identity.set(("alice", "admins"))
    exchange = _FakeExchange()
    exchange.install(monkeypatch)

    box = McpToolbox("http://mcp.test/mcp", "/nonexistent-token-file", "")
    assert await box._exchange_token() is False
    assert exchange.calls == []


@pytest.mark.asyncio
async def test_exchange_populates_session_token(monkeypatch):
    """A successful exchange caches the minted token so the next connection
    presents it (and it's preferred over the deploy-time file)."""
    current_admin_identity.set(("bob", "admins"))
    exchange = _FakeExchange(token="sb_bob_tok")
    exchange.install(monkeypatch)

    box = McpToolbox("http://mcp.test/mcp", "/nonexistent-token-file", SB_API)
    assert await box._exchange_token() is True
    assert box._session_token == "sb_bob_tok"
    assert box._headers() == {"Authorization": "Bearer sb_bob_tok"}

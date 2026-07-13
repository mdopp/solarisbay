"""A stale admin SB-MCP token (#794) must self-heal at runtime.

The token file is written only at deploy time, but ServiceBay's token pool
rotates, so a long-lived engine eventually 401s on /mcp. `McpToolbox` re-mints
the token on a 401 (reusing the post-deploy's /api/system/api-tokens mint) and
retries once — no redeploy. A 401 surfaces from the MCP streamable-http client
wrapped in an ExceptionGroup (anyio task group), so the detection must unwrap it.
"""

from __future__ import annotations

import json

import pytest

from solaris_chat.engine.tools import mcp_tools
from solaris_chat.engine.tools.mcp_tools import McpToolbox, _is_401

GOOD = "sb_0123abcd_ABCDEFG234567"
NEW = "sb_99999999_NEWTOKEN234567"


class _Resp:
    def __init__(self, status_code: int):
        self.status_code = status_code


class _Http401(Exception):
    """A stand-in for httpx.HTTPStatusError — duck-typed on `.response`."""

    def __init__(self):
        super().__init__("401 Unauthorized")
        self.response = _Resp(401)


def _wrapped_401() -> BaseException:
    """A 401 as it reaches prepare(): wrapped in a task-group ExceptionGroup."""
    return BaseExceptionGroup("unhandled errors in a TaskGroup", [_Http401()])


# -- 401 detection unwraps the ExceptionGroup ---------------------------------


def test_is_401_unwraps_exception_group():
    assert _is_401(_wrapped_401())
    assert _is_401(_Http401())


def test_is_401_false_for_other_errors():
    assert not _is_401(RuntimeError("boom"))
    assert not _is_401(BaseExceptionGroup("g", [RuntimeError("boom")]))


# -- re-mint POST stub --------------------------------------------------------


class _FakePost:
    def __init__(self, status: int, body: dict):
        self._status = status
        self._body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    @property
    def status(self):
        return self._status

    async def json(self):
        return self._body


class _FakeSession:
    """Records the re-mint POST and returns a canned response."""

    calls: list[tuple[str, dict, dict]] = []

    def __init__(self, status: int, body: dict):
        self._status = status
        self._body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def post(self, url, json=None, headers=None):  # noqa: A002
        type(self).calls.append((url, json, headers))
        return _FakePost(self._status, self._body)


def _install_fake_session(monkeypatch, status: int, body: dict):
    _FakeSession.calls = []

    def factory(*a, **kw):
        return _FakeSession(status, body)

    monkeypatch.setattr(mcp_tools.aiohttp, "ClientSession", factory)


# -- prepare(): 401 on first list → re-mint → retry recovers ------------------


@pytest.mark.asyncio
async def test_401_on_list_triggers_remint_and_retry(tmp_path, monkeypatch):
    token_file = tmp_path / "sb-admin-token"
    token_file.write_text(GOOD + "\n")
    _install_fake_session(monkeypatch, 200, {"secret": NEW})

    box = McpToolbox(
        "http://mcp",
        str(token_file),
        sb_api_url="http://sb:3000",
        sb_api_token="internal-secret",
    )

    seen: list[str] = []

    async def fake_list():
        # The first probe reads the stale token and 401s; the retry sees the
        # freshly minted one and succeeds.
        current = token_file.read_text().strip()
        seen.append(current)
        if current == GOOD:
            raise _wrapped_401()
        return [{"name": "list_services", "description": "d", "inputSchema": {}}]

    monkeypatch.setattr(box, "_list_tools", fake_list)

    await box.prepare()

    # It re-minted (POST to the canonical route with the internal-token header)
    url, payload, headers = _FakeSession.calls[0]
    assert url == "http://sb:3000/api/system/api-tokens"
    assert payload["scopes"] == ["read", "lifecycle", "mutate"]
    assert headers["X-SB-Internal-Token"] == "internal-secret"
    # The token file now carries the new token, 0600.
    assert token_file.read_text().strip() == NEW
    assert (token_file.stat().st_mode & 0o777) == 0o600
    # And the tool list recovered (the retry succeeded).
    assert box.names() == ["list_services"]
    assert seen == [GOOD, NEW]


@pytest.mark.asyncio
async def test_no_remint_without_sb_api_creds_stays_fail_open(tmp_path, monkeypatch):
    token_file = tmp_path / "sb-admin-token"
    token_file.write_text(GOOD + "\n")
    _install_fake_session(monkeypatch, 200, {"secret": NEW})

    # No sb_api_url/token ⇒ can't re-mint; a 401 must stay fail-open (no tools),
    # never raise, and never POST.
    box = McpToolbox("http://mcp", str(token_file))

    async def always_401():
        raise _wrapped_401()

    monkeypatch.setattr(box, "_list_tools", always_401)

    await box.prepare()

    assert box.names() == []
    assert _FakeSession.calls == []
    assert token_file.read_text().strip() == GOOD


@pytest.mark.asyncio
async def test_remint_rejects_non_sb_shaped_secret(tmp_path, monkeypatch):
    token_file = tmp_path / "sb-admin-token"
    token_file.write_text(GOOD + "\n")
    _install_fake_session(monkeypatch, 200, {"secret": "not-a-token"})

    box = McpToolbox(
        "http://mcp",
        str(token_file),
        sb_api_url="http://sb:3000",
        sb_api_token="internal-secret",
    )

    async def always_401():
        raise _wrapped_401()

    monkeypatch.setattr(box, "_list_tools", always_401)

    await box.prepare()

    # A junk secret is never written; the stale token is left untouched.
    assert token_file.read_text().strip() == GOOD
    assert box.names() == []


@pytest.mark.asyncio
async def test_401_on_dispatch_triggers_remint_and_retry(tmp_path, monkeypatch):
    token_file = tmp_path / "sb-admin-token"
    token_file.write_text(GOOD + "\n")
    _install_fake_session(monkeypatch, 200, {"secret": NEW})

    box = McpToolbox(
        "http://mcp",
        str(token_file),
        sb_api_url="http://sb:3000",
        sb_api_token="internal-secret",
    )
    box._names = ["list_services"]

    async def fake_call(name, arguments):
        if token_file.read_text().strip() == GOOD:
            raise _wrapped_401()
        return json.dumps({"ok": True, "tool": name})

    monkeypatch.setattr(box, "_call_tool", fake_call)

    out = await box.dispatch("list_services", {})

    assert token_file.read_text().strip() == NEW
    assert json.loads(out) == {"ok": True, "tool": "list_services"}

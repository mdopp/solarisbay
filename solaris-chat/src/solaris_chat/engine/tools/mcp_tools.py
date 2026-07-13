"""ServiceBay MCP tools for the admin profile — official `mcp` SDK client.

The admin persona's operator powers come from the `servicebay_admin` MCP
endpoint (token scopes read+lifecycle+mutate, no destroy/exec — unchanged
from the Hermes era).

Token source (#794): NOT a standing minting credential in the pod. The SB-MCP
Bearer is minted on demand from the ACTING admin's *live, verified* Authelia
session — the engine forwards the admin's forward-auth identity
(Remote-User / Remote-Groups, pinned per turn in `current_admin_identity`) to
ServiceBay's `POST /api/auth/token-from-authelia-session`, which returns a
short-lived (≤1h) read+lifecycle+mutate token. Authority flows from the human
signed in behind NPM, so the pod holds no long-lived token-minting secret. The
deploy-time token file (`sb_mcp_token_path`) remains a fallback for the boot/
code path (onboarding via `call_sb_tool`); the admin chat prefers the
session-exchanged token and re-exchanges it on a 401.

Connections are per-call (connect → initialize → act → close): admin turns
are rare and the MCP server is loopback, so holding a long-lived session
buys nothing and costs reconnect handling. Fail-open everywhere — an
unreachable MCP server leaves the admin chat tool-less, never broken.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import aiohttp

from solaris_chat.engine.client import current_admin_identity
from solaris_chat.engine.tools import Toolbox
from solaris_chat.logging import log

_TTL_S = 300.0
_EXCHANGE_TIMEOUT = aiohttp.ClientTimeout(total=15)
# The Authelia-session → SB-MCP token exchange (servicebay#2246). Mints a
# short-lived read+lifecycle+mutate token from the caller's forward-auth
# identity; refuses without Remote-User (401) or a client Bearer (403).
_EXCHANGE_PATH = "/api/auth/token-from-authelia-session"


def read_token(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _is_401(exc: BaseException) -> bool:
    """True when `exc` is (or, for a task-group ExceptionGroup, wraps) an HTTP
    401. The MCP streamable-http client runs in an anyio task group, so a stale
    token surfaces as an ExceptionGroup around an httpx HTTPStatusError; we
    duck-type on `.response.status_code` to avoid a direct httpx import."""
    if isinstance(exc, BaseExceptionGroup):
        return any(_is_401(sub) for sub in exc.exceptions)
    response = getattr(exc, "response", None)
    return getattr(response, "status_code", None) == 401


class McpToolbox(Toolbox):
    def __init__(self, url: str, token_path: str, sb_api_url: str = ""):
        super().__init__([])
        self._url = url
        self._token_path = token_path
        # SB control-plane base for the Authelia-session token exchange (#794).
        # Empty ⇒ no session exchange (the deploy-time token file is all we
        # have); a rotation then needs a redeploy.
        self._sb_api_url = sb_api_url.rstrip("/")
        # The short-lived token minted from the acting admin's session, cached
        # in-memory between the exchange and the connection that uses it. Never
        # persisted — it dies with the process (and expires ≤1h anyway).
        self._session_token = ""
        self._defs: list[dict[str, Any]] = []
        self._names: list[str] = []
        self._fetched_at = 0.0

    @property
    def url(self) -> str:
        return self._url

    async def prepare(self) -> None:
        if not self._url:
            return
        if self._defs and (time.time() - self._fetched_at) < _TTL_S:
            return
        try:
            tools = await self._list_tools()
        except Exception as e:  # noqa: BLE001 — fail-open: stale beats broken
            if _is_401(e) and await self._exchange_token():
                try:
                    tools = await self._list_tools()
                except Exception as e2:  # noqa: BLE001
                    log.warn("engine.mcp.list_failed", url=self._url, error=str(e2))
                    return
            else:
                log.warn("engine.mcp.list_failed", url=self._url, error=str(e))
                return
        self._defs = [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t.get("description") or "",
                    "parameters": t.get("inputSchema")
                    or {"type": "object", "properties": {}},
                },
            }
            for t in tools
        ]
        self._names = [t["name"] for t in tools]
        self._fetched_at = time.time()
        log.info("engine.mcp.tools", url=self._url, n=len(self._names))

    def definitions(self) -> list[dict[str, Any]]:
        return list(self._defs)

    def names(self) -> list[str]:
        return list(self._names)

    async def dispatch(self, name: str, arguments: dict[str, Any]) -> str:
        if name not in self._names:
            return f'{{"error": "unknown tool: {name}"}}'
        try:
            return await self._call_tool(name, arguments)
        except Exception as e:  # noqa: BLE001 — a tool error is model feedback
            if _is_401(e) and await self._exchange_token():
                try:
                    return await self._call_tool(name, arguments)
                except Exception as e2:  # noqa: BLE001
                    e = e2
            return f'{{"error": "{type(e).__name__}: {str(e)[:200]}"}}'

    # -- MCP wire ------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        # Prefer the token exchanged from the acting admin's Authelia session
        # (#794); fall back to the deploy-time file for the boot/code path.
        token = self._session_token or read_token(self._token_path)
        return {"Authorization": f"Bearer {token}"} if token else {}

    async def _exchange_token(self) -> bool:
        """Mint a fresh SB-MCP token from the ACTING admin's live Authelia
        session (#794): forward the turn's Remote-User / Remote-Groups
        forward-auth headers to `token-from-authelia-session`, which returns a
        short-lived read+lifecycle+mutate token. NO standing minting credential
        and NO client Bearer is sent (the endpoint 403s a token caller). Only an
        admin turn carries an identity, so a non-admin turn exchanges nothing.
        Best-effort: no SB API base or no admin identity ⇒ False; any failure ⇒
        False (the caller stays fail-open). True only when a token was cached."""
        if not self._sb_api_url:
            return False
        user, groups = current_admin_identity.get()
        if not user:
            return False
        try:
            async with aiohttp.ClientSession(timeout=_EXCHANGE_TIMEOUT) as client:
                async with client.post(
                    f"{self._sb_api_url}{_EXCHANGE_PATH}",
                    headers={"Remote-User": user, "Remote-Groups": groups},
                ) as resp:
                    if resp.status != 200:
                        log.warn("engine.mcp.exchange_failed", status=resp.status)
                        return False
                    body = await resp.json()
        except (aiohttp.ClientError, ValueError) as e:
            log.warn("engine.mcp.exchange_failed", error=str(e))
            return False
        token = body.get("token") if isinstance(body, dict) else None
        if not isinstance(token, str) or not token:
            log.warn("engine.mcp.exchange_failed", reason="no token in response")
            return False
        self._session_token = token
        log.info("engine.mcp.exchanged", url=self._url, user=user)
        return True

    async def _list_tools(self) -> list[dict[str, Any]]:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        async with streamablehttp_client(self._url, headers=self._headers()) as (
            read,
            write,
            _,
        ):
            async with ClientSession(read, write) as session:
                await session.initialize()
                listed = await session.list_tools()
        return [
            {
                "name": t.name,
                "description": t.description,
                "inputSchema": t.inputSchema,
            }
            for t in listed.tools
        ]

    async def _call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        async with streamablehttp_client(self._url, headers=self._headers()) as (
            read,
            write,
            _,
        ):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(name, arguments)
        parts: list[str] = []
        for item in result.content:
            text = getattr(item, "text", None)
            if text:
                parts.append(str(text))
        out = "\n".join(parts) or json.dumps({"ok": not result.isError})
        return out[:16000]


class CombinedToolbox(Toolbox):
    """Two toolboxes presented as one — the admin profile needs the remote
    SB-MCP tools *and* a few local onboarding tools (#355) in one toolset.
    A name in the first wins on dispatch (none currently collide)."""

    def __init__(self, *boxes: Toolbox):
        super().__init__([])
        self._boxes = list(boxes)

    async def prepare(self) -> None:
        for box in self._boxes:
            await box.prepare()

    def definitions(self) -> list[dict[str, Any]]:
        defs: list[dict[str, Any]] = []
        for box in self._boxes:
            defs += box.definitions()
        return defs

    def names(self) -> list[str]:
        names: list[str] = []
        for box in self._boxes:
            names += box.names()
        return names

    async def dispatch(self, name: str, arguments: dict[str, Any]) -> str:
        for box in self._boxes:
            if name in box.names():
                return await box.dispatch(name, arguments)
        return f'{{"error": "unknown tool: {name}"}}'


async def call_sb_tool(
    url: str, token_path: str, name: str, arguments: dict[str, Any]
) -> str:
    """Invoke one SB-MCP tool over the same connect→initialize→act→close path
    the admin toolbox uses, but callable from Python (the #355 onboarding flow
    needs to file/poll an access request as a code side-effect, not only expose
    the tool to the model)."""
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    token = read_token(token_path)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with streamablehttp_client(url, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(name, arguments)
    parts: list[str] = []
    for item in result.content:
        text = getattr(item, "text", None)
        if text:
            parts.append(str(text))
    out = "\n".join(parts) or json.dumps({"ok": not result.isError})
    return out[:16000]

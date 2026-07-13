"""ServiceBay MCP tools for the admin profile — official `mcp` SDK client.

The admin persona's operator powers come from the `servicebay_admin` MCP
endpoint (token scopes read+lifecycle+mutate, no destroy/exec — unchanged
from the Hermes era). The token is minted by the post-deploy and dropped as
a file on the solaris-data volume, so it is read lazily per connection: a
token minted after the chat server booted works without a restart.

Connections are per-call (connect → initialize → act → close): admin turns
are rare and the MCP server is loopback, so holding a long-lived session
buys nothing and costs reconnect handling. Fail-open everywhere — an
unreachable MCP server leaves the admin chat tool-less, never broken.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

import aiohttp

from solaris_chat.engine.tools import Toolbox
from solaris_chat.logging import log

_TTL_S = 300.0

# A ServiceBay-minted MCP token is `sb_<8-hex-id>_<base32-ish-secret>` — the same
# shape the post-deploy's mint_admin_token accepts (any other value is a
# permanent 401). We validate a freshly minted secret against it before writing.
_SB_MCP_TOKEN_RE = re.compile(r"^sb_[0-9a-f]{8}_[A-Z2-9]+$")
_ADMIN_TOKEN_NAME = "admin-soul"
_ADMIN_MCP_SCOPES = ["read", "lifecycle", "mutate"]
_REMINT_TIMEOUT = aiohttp.ClientTimeout(total=15)


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
    def __init__(
        self,
        url: str,
        token_path: str,
        sb_api_url: str = "",
        sb_api_token: str = "",
    ):
        super().__init__([])
        self._url = url
        self._token_path = token_path
        # SB control-plane endpoint + internal token used to re-mint the admin
        # SB-MCP token when it rotates stale (#794). Empty ⇒ no runtime re-mint
        # (the deploy-time token is all we have; a rotation needs a redeploy).
        self._sb_api_url = sb_api_url.rstrip("/")
        self._sb_api_token = sb_api_token
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
            if _is_401(e) and await self._remint():
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
            if _is_401(e) and await self._remint():
                try:
                    return await self._call_tool(name, arguments)
                except Exception as e2:  # noqa: BLE001
                    e = e2
            return f'{{"error": "{type(e).__name__}: {str(e)[:200]}"}}'

    # -- MCP wire ------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        token = read_token(self._token_path)
        return {"Authorization": f"Bearer {token}"} if token else {}

    async def _remint(self) -> bool:
        """Re-mint the admin SB-MCP token when the stored one rotated stale
        (#794): the token file is written only at deploy time, but SB's token
        pool rotates, so a long-lived engine eventually 401s. Mint a fresh
        read+lifecycle+mutate token via the SB control-plane API (the same
        `/api/system/api-tokens` route + `X-SB-Internal-Token` the post-deploy's
        mint_admin_token uses) and overwrite the token file (0600) so the next
        connection reads it. Best-effort: no SB API URL/token ⇒ can't re-mint,
        and any failure returns False (the caller stays fail-open). Returns True
        only when a valid new token was written."""
        if not self._sb_api_url or not self._sb_api_token:
            return False
        try:
            async with aiohttp.ClientSession(timeout=_REMINT_TIMEOUT) as client:
                async with client.post(
                    f"{self._sb_api_url}/api/system/api-tokens",
                    json={"name": _ADMIN_TOKEN_NAME, "scopes": _ADMIN_MCP_SCOPES},
                    headers={"X-SB-Internal-Token": self._sb_api_token},
                ) as resp:
                    if resp.status != 200:
                        log.warn("engine.mcp.remint_failed", status=resp.status)
                        return False
                    body = await resp.json()
        except (aiohttp.ClientError, ValueError) as e:
            log.warn("engine.mcp.remint_failed", error=str(e))
            return False
        secret = body.get("secret") if isinstance(body, dict) else None
        if not (isinstance(secret, str) and _SB_MCP_TOKEN_RE.match(secret)):
            log.warn("engine.mcp.remint_failed", reason="non-sb-shaped secret")
            return False
        try:
            path = Path(self._token_path)
            path.write_text(secret + "\n", encoding="utf-8")
            path.chmod(0o600)
        except OSError as e:
            log.warn("engine.mcp.remint_failed", error=str(e))
            return False
        log.info("engine.mcp.reminted", url=self._url)
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
        return await call_sb_tool(self._url, self._token_path, name, arguments)


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

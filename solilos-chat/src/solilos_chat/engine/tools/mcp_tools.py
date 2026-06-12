"""ServiceBay MCP tools for the admin profile — official `mcp` SDK client.

The admin persona's operator powers come from the `servicebay_admin` MCP
endpoint (token scopes read+lifecycle+mutate, no destroy/exec — unchanged
from the Hermes era). The token is minted by the post-deploy and dropped as
a file on the solilos-data volume, so it is read lazily per connection: a
token minted after the chat server booted works without a restart.

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

from solilos_chat.engine.tools import Toolbox
from solilos_chat.logging import log

_TTL_S = 300.0


def read_token(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


class McpToolbox(Toolbox):
    def __init__(self, url: str, token_path: str):
        super().__init__([])
        self._url = url
        self._token_path = token_path
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
            return f'{{"error": "{type(e).__name__}: {str(e)[:200]}"}}'

    # -- MCP wire ------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        token = read_token(self._token_path)
        return {"Authorization": f"Bearer {token}"} if token else {}

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

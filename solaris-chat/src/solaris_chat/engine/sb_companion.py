"""ServiceBay companion-read client — the BFF read side (ADR 0010, #811).

Solaris is the BFF/hub: the app talks only to Solaris, never to ServiceBay
directly. ServiceBay already exposes a token-only, proxy-bypassed companion-read
surface for the app (servicebay#2252) — `GET /napi/{home,approvals,services,
upgrades}`. This client consumes those four reads server-to-server so Solaris can
re-serve them under its OWN `/napi/servicebay/*` to the paired device.

Credentials: these reads are `read`-scoped Bearer (servicebay#2252). The client
runs with no acting admin session, so it reads the deploy-time SB-MCP token file
(`sb_mcp_token_path`, read+lifecycle+mutate) — the SAME token the update/approval
pollers use for their unattended polls, NOT a second credential.

Fail-soft: an unreachable ServiceBay, a non-200, or malformed JSON returns None
so the caller can turn it into a 502 for the app rather than crash.
"""

from __future__ import annotations

from typing import Any

import aiohttp

from solaris_chat.engine.tools.mcp_tools import read_token
from solaris_chat.logging import log

_TIMEOUT = aiohttp.ClientTimeout(total=20)

# The four companion-read routes ServiceBay exposes (servicebay#2252). Keyed by
# the path segment the app asks Solaris for, so `/napi/servicebay/<key>` maps 1:1.
READ_PATHS = {
    "home": "/napi/home",
    "approvals": "/napi/approvals",
    "services": "/napi/services",
    "upgrades": "/napi/upgrades",
}


class SbCompanionClient:
    def __init__(self, sb_api_url: str, sb_mcp_token_path: str):
        self._base = sb_api_url.rstrip("/")
        self._token_path = sb_mcp_token_path

    @property
    def enabled(self) -> bool:
        return bool(self._base)

    async def read(self, key: str) -> dict[str, Any] | None:
        """Fetch one companion read (`home`/`approvals`/`services`/`upgrades`).

        Returns ServiceBay's JSON body verbatim (the app renders it directly), or
        None when SB is unreachable / non-200 / malformed / unknown key."""
        path = READ_PATHS.get(key)
        if path is None or not self._base:
            return None
        token = read_token(self._token_path)
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        url = f"{self._base}{path}"
        try:
            async with aiohttp.ClientSession(timeout=_TIMEOUT) as client:
                async with client.get(url, headers=headers) as resp:
                    if resp.status != 200:
                        log.warn(
                            "engine.sb_companion.http", key=key, status=resp.status
                        )
                        return None
                    return await resp.json()
        except (aiohttp.ClientError, ValueError, TimeoutError, OSError) as e:
            log.warn("engine.sb_companion.fetch_failed", key=key, error=str(e))
            return None

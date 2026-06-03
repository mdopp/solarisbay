"""Entry point — run the Audiobookshelf MCP server."""

from __future__ import annotations

import asyncio

from .config import settings
from .logging import log
from .server import serve


async def _serve() -> None:
    await serve(
        base_url=settings.abs_base_url,
        api_key=settings.abs_api_key,
        host=settings.mcp_host,
        port=settings.mcp_port,
        token=settings.mcp_token,
    )


def main() -> None:
    if not settings.abs_api_key:
        # Start anyway so the container is up and registerable, but make the
        # missing credential loud — every tool call will return
        # abs_unavailable (401 from ABS) until ABS_API_KEY is set.
        log.warn("abs.mcp.no_api_key", abs_base_url=settings.abs_base_url)
    asyncio.run(_serve())


if __name__ == "__main__":
    main()

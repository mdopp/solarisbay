"""MCP tool server exposing read-only Audiobookshelf library lookups.

Two tools, both read-only:

  * `abs_search(query, limit)`    — search the book libraries.
  * `abs_availability(title, …)`  — "do we already own this?" convenience
                                     used after a book is ingested (#89).

There is deliberately no write/destructive tool here — the shim only ever
issues GETs against Audiobookshelf, so even though it holds the ABS
credential, a prompt-injected agent cannot mutate the library through it.

Runs as a streamable-HTTP ASGI app on `MCP_PORT`. `solbay`'s
post-deploy registers it in Hermes' `mcp_servers:` block.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from .abs_client import AbsClient
from .logging import log

_MAX_LIMIT = 25


def build_mcp(*, client: AbsClient) -> FastMCP:
    """Construct the FastMCP server with the lookup tools. Pure factory so
    tests can drive the tools against a fake client without binding a port."""
    mcp = FastMCP("solilos-abs")

    @mcp.tool()
    async def abs_search(query: str, limit: int = 5) -> dict:
        """Search the household's Audiobookshelf book libraries.

        Returns matching items as `{title, author, library, item_id}`. Use
        this to answer "do we have <book>?" style questions about the
        physical/audiobook collection. Read-only.
        """
        q = query.strip()
        if not q:
            return {"ok": False, "reason": "empty_query"}
        limit = max(1, min(int(limit), _MAX_LIMIT))
        try:
            hits = await client.search(q, limit=limit)
        except Exception as exc:  # noqa: BLE001 — ABS down / auth / shape
            log.error("abs.search_error", query=q, error=str(exc))
            return {"ok": False, "reason": "abs_unavailable"}
        results = [
            {
                "title": h.title,
                "author": h.author,
                "library": h.library,
                "item_id": h.item_id,
            }
            for h in hits
        ]
        return {"ok": True, "query": q, "count": len(results), "results": results}

    @mcp.tool()
    async def abs_availability(title: str, author: str = "") -> dict:
        """Check whether a title is already in Audiobookshelf.

        Pass the book/album `title` (and `author` if known) right after
        ingesting a note, to tell the resident whether it's already in the
        digital library. Returns `{available, matches}`. Read-only.
        """
        t = title.strip()
        if not t:
            return {"ok": False, "reason": "empty_title"}
        query = f"{t} {author.strip()}".strip()
        try:
            hits = await client.search(query, limit=5)
        except Exception as exc:  # noqa: BLE001 — ABS down / auth / shape
            log.error("abs.availability_error", title=t, error=str(exc))
            return {"ok": False, "reason": "abs_unavailable"}
        matches = [
            {"title": h.title, "author": h.author, "library": h.library} for h in hits
        ]
        return {
            "ok": True,
            "title": t,
            "available": bool(matches),
            "matches": matches,
        }

    return mcp


class _BearerAuth:
    """Pure-ASGI bearer gate. Empty token = open, which is safe for the
    default loopback bind (`MCP_HOST=127.0.0.1`); only Hermes on the same
    host reaches it. Pure ASGI rather than BaseHTTPMiddleware so it doesn't
    buffer the streamable-HTTP/SSE responses. Mirrors the gatekeeper
    room-MCP gate."""

    def __init__(self, app: ASGIApp, token: str) -> None:
        self._app = app
        self._token = token

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and self._token:
            headers = dict(scope.get("headers") or [])
            if headers.get(b"authorization", b"").decode() != f"Bearer {self._token}":
                await JSONResponse(
                    {"ok": False, "reason": "unauthorized"}, status_code=401
                )(scope, receive, send)
                return
        await self._app(scope, receive, send)


async def serve(
    *, base_url: str, api_key: str, host: str, port: int, token: str = ""
) -> None:
    """Run the ABS MCP server forever."""
    import uvicorn

    client = AbsClient(base_url, api_key)
    mcp = build_mcp(client=client)
    app = _BearerAuth(mcp.streamable_http_app(), token)
    config = uvicorn.Config(
        app, host=host, port=port, log_level="warning", lifespan="on"
    )
    log.info(
        "abs.mcp.listening",
        host=host,
        port=port,
        abs_base_url=base_url,
        auth=bool(token),
        abs_key=bool(api_key),
    )
    await uvicorn.Server(config).serve()

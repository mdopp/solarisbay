"""Tests for the Audiobookshelf MCP tool server."""

from __future__ import annotations

import json

import httpx
from mcp.shared.memory import create_connected_server_and_client_session as connect

from abs_mcp.abs_client import BookHit
from abs_mcp.server import _BearerAuth, build_mcp


class FakeClient:
    """Stands in for AbsClient — records the last query, returns canned
    hits, or raises to exercise the degraded path."""

    def __init__(self, hits=None, raises=False):
        self._hits = hits or []
        self._raises = raises
        self.last_query = None
        self.last_limit = None

    async def search(self, query, *, limit=5):
        self.last_query = query
        self.last_limit = limit
        if self._raises:
            raise RuntimeError("abs down")
        return self._hits


def _payload(result):
    return json.loads(result.content[0].text)


_HITS = [
    BookHit(item_id="li_1", title="Dune", author="Frank Herbert", library="Books"),
    BookHit(
        item_id="li_2", title="Dune Messiah", author="Frank Herbert", library="Books"
    ),
]


async def test_lists_abs_tools():
    async with connect(build_mcp(client=FakeClient())._mcp_server) as client:
        tools = await client.list_tools()
        assert {t.name for t in tools.tools} == {"abs_search", "abs_availability"}


async def test_search_returns_hits():
    fake = FakeClient(hits=_HITS)
    async with connect(build_mcp(client=fake)._mcp_server) as client:
        res = _payload(await client.call_tool("abs_search", {"query": "dune"}))
    assert res["ok"] is True
    assert res["count"] == 2
    assert res["results"][0] == {
        "title": "Dune",
        "author": "Frank Herbert",
        "library": "Books",
        "item_id": "li_1",
    }
    assert fake.last_query == "dune"


async def test_search_rejects_empty_query():
    async with connect(build_mcp(client=FakeClient())._mcp_server) as client:
        res = _payload(await client.call_tool("abs_search", {"query": "  "}))
    assert res == {"ok": False, "reason": "empty_query"}


async def test_search_clamps_limit():
    fake = FakeClient(hits=_HITS)
    async with connect(build_mcp(client=fake)._mcp_server) as client:
        await client.call_tool("abs_search", {"query": "dune", "limit": 999})
    assert fake.last_limit == 25


async def test_search_degrades_when_abs_unavailable():
    async with connect(build_mcp(client=FakeClient(raises=True))._mcp_server) as client:
        res = _payload(await client.call_tool("abs_search", {"query": "dune"}))
    assert res == {"ok": False, "reason": "abs_unavailable"}


async def test_availability_true_with_matches():
    fake = FakeClient(hits=_HITS)
    async with connect(build_mcp(client=fake)._mcp_server) as client:
        res = _payload(
            await client.call_tool(
                "abs_availability", {"title": "Dune", "author": "Frank Herbert"}
            )
        )
    assert res["ok"] is True
    assert res["available"] is True
    assert res["matches"][0]["title"] == "Dune"
    assert fake.last_query == "Dune Frank Herbert"


async def test_availability_false_when_no_hits():
    async with connect(build_mcp(client=FakeClient(hits=[]))._mcp_server) as client:
        res = _payload(await client.call_tool("abs_availability", {"title": "Nope"}))
    assert res["ok"] is True
    assert res["available"] is False
    assert res["matches"] == []


async def test_availability_rejects_empty_title():
    async with connect(build_mcp(client=FakeClient())._mcp_server) as client:
        res = _payload(await client.call_tool("abs_availability", {"title": ""}))
    assert res == {"ok": False, "reason": "empty_title"}


# ── bearer auth middleware ───────────────────────────────────────────────────


async def _ok_app(scope, receive, send):
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


async def test_bearer_auth_open_when_token_empty():
    transport = httpx.ASGITransport(app=_BearerAuth(_ok_app, ""))
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        assert (await c.get("/mcp")).status_code == 200


async def test_bearer_auth_enforced_when_token_set():
    transport = httpx.ASGITransport(app=_BearerAuth(_ok_app, "secret"))
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        assert (await c.get("/mcp")).status_code == 401
        assert (
            await c.get("/mcp", headers={"Authorization": "Bearer secret"})
        ).status_code == 200

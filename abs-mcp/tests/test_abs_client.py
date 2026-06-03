"""Tests for the defensive Audiobookshelf REST parsing."""

from __future__ import annotations

import httpx
import pytest

from abs_mcp.abs_client import AbsClient

_LIBRARIES = {
    "libraries": [
        {"id": "lib-books", "name": "Books", "mediaType": "book"},
        {"id": "lib-pods", "name": "Podcasts", "mediaType": "podcast"},
    ]
}

_SEARCH = {
    "book": [
        {
            "libraryItem": {
                "id": "li_dune",
                "media": {"metadata": {"title": "Dune", "authorName": "Frank Herbert"}},
            },
            "matchKey": "title",
        }
    ],
    "authors": [{"name": "Frank Herbert"}],
}


def _handler(request: httpx.Request) -> httpx.Response:
    assert request.headers.get("Authorization") == "Bearer key123"
    if request.url.path == "/api/libraries":
        return httpx.Response(200, json=_LIBRARIES)
    if request.url.path == "/api/libraries/lib-books/search":
        assert request.url.params.get("q") == "dune"
        return httpx.Response(200, json=_SEARCH)
    return httpx.Response(404, json={})


@pytest.fixture
def patched_async_client(monkeypatch):
    """Force httpx.AsyncClient to use a MockTransport regardless of the
    kwargs AbsClient passes (timeout=…)."""
    real_init = httpx.AsyncClient.__init__

    def init(self, *args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(_handler)
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", init)


async def test_search_only_queries_book_libraries(patched_async_client):
    client = AbsClient("http://abs:13378", "key123")
    hits = await client.search("dune", limit=3)
    assert len(hits) == 1
    h = hits[0]
    assert (h.item_id, h.title, h.author, h.library) == (
        "li_dune",
        "Dune",
        "Frank Herbert",
        "Books",
    )


async def test_author_falls_back_to_authors_list(monkeypatch):
    search = {
        "book": [
            {
                "libraryItem": {
                    "id": "li_x",
                    "media": {
                        "metadata": {
                            "title": "X",
                            "authors": [{"name": "A. One"}, {"name": "B. Two"}],
                        }
                    },
                }
            }
        ]
    }

    def handler(request):
        if request.url.path == "/api/libraries":
            return httpx.Response(200, json=_LIBRARIES)
        return httpx.Response(200, json=search)

    real_init = httpx.AsyncClient.__init__

    def init(self, *args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", init)
    hits = await AbsClient("http://abs:13378", "key123").search("x")
    assert hits[0].author == "A. One, B. Two"

"""Thin async client for the Audiobookshelf REST API.

Only the read endpoints the MCP tools need: list libraries and search a
library. Parsing is deliberately defensive — Audiobookshelf's public API
docs are stale/incomplete on the search shape, so we pull fields out of
the response with `.get(...)` chains and tolerate absences rather than
assume a rigid schema. The shape is pinned against the live instance
during box verification (see README).
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx


@dataclass(frozen=True)
class BookHit:
    item_id: str
    title: str
    author: str
    library: str


def _metadata(library_item: dict) -> dict:
    media = library_item.get("media")
    if isinstance(media, dict):
        meta = media.get("metadata")
        if isinstance(meta, dict):
            return meta
    return {}


def _author_of(meta: dict) -> str:
    # ABS exposes a flattened `authorName` on book metadata; fall back to
    # the structured `authors: [{name}]` list if it's absent.
    name = meta.get("authorName")
    if isinstance(name, str) and name:
        return name
    authors = meta.get("authors")
    if isinstance(authors, list):
        names = [a.get("name", "") for a in authors if isinstance(a, dict)]
        return ", ".join(n for n in names if n)
    return ""


class AbsClient:
    def __init__(self, base_url: str, api_key: str, timeout: float = 10.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._timeout = timeout

    async def _get(
        self, client: httpx.AsyncClient, path: str, **params: object
    ) -> dict:
        resp = await client.get(
            f"{self._base_url}{path}", headers=self._headers, params=params or None
        )
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, dict) else {}

    async def book_libraries(self, client: httpx.AsyncClient) -> list[tuple[str, str]]:
        """Return `(library_id, library_name)` for every book-type library."""
        data = await self._get(client, "/api/libraries")
        out: list[tuple[str, str]] = []
        for lib in data.get("libraries", []):
            if isinstance(lib, dict) and lib.get("mediaType") == "book":
                lib_id = lib.get("id")
                if lib_id:
                    out.append((str(lib_id), str(lib.get("name", lib_id))))
        return out

    async def search(self, query: str, *, limit: int = 5) -> list[BookHit]:
        """Search every book library for `query`, newest-match-first per
        library, capped at `limit` hits per library."""
        hits: list[BookHit] = []
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            for lib_id, lib_name in await self.book_libraries(client):
                data = await self._get(
                    client, f"/api/libraries/{lib_id}/search", q=query, limit=limit
                )
                for entry in data.get("book", []):
                    if not isinstance(entry, dict):
                        continue
                    item = entry.get("libraryItem")
                    if not isinstance(item, dict):
                        continue
                    meta = _metadata(item)
                    hits.append(
                        BookHit(
                            item_id=str(item.get("id", "")),
                            title=str(meta.get("title", "")),
                            author=_author_of(meta),
                            library=lib_name,
                        )
                    )
        return hits

"""Read-only Immich source client for the ingest adapter.

The adapter depends on the `ImmichClient` Protocol, not the concrete REST
client — so tests inject a fake and the live path uses `RestImmichClient`
(thin aiohttp wrapper over the Immich REST API, `IMMICH_BASE_URL` +
`IMMICH_API_KEY`). Read-only on the source: only `GET`/search `POST` calls.

The dataclasses are the normalized subset the adapter maps from. The REST
client folds Immich's JSON into them so the adapter never touches raw payload
quirks; a fake client returns the same dataclasses directly.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Protocol

import aiohttp

from ...logging import log

# A keep-alive drop mid-paging must not kill a 76k-asset run (#597): re-issue the
# SAME page with exponential backoff before giving up.
_PAGE_RETRY_BACKOFF = (0.5, 1.0, 2.0, 4.0)  # seconds — 4 retries after the first try.


@dataclass(frozen=True)
class ImmichPerson:
    """An Immich-identified face/person on an asset (already named by Immich)."""

    id: str
    name: str


@dataclass(frozen=True)
class ImmichAsset:
    """The normalized subset of an Immich asset the adapter ingests.

    `shared_with` is the set of resident uids the asset is shared with (album /
    shared-asset membership, §6); empty ⇒ default scope (ingesting resident).
    """

    id: str
    file_name: str
    when: str  # ISO-8601 capture time (EXIF dateTimeOriginal / fileCreatedAt).
    checksum: str  # Immich's content checksum — the change key for content_hash.
    latitude: float | None = None
    longitude: float | None = None
    city: str = ""
    state: str = ""
    country: str = ""
    people: list[ImmichPerson] = field(default_factory=list)
    shared_with: list[str] = field(default_factory=list)


class ImmichClient(Protocol):
    """Read-only Immich access the adapter needs. Injectable for tests."""

    def iter_assets(self, *, updated_after: str = "") -> AsyncIterator[ImmichAsset]:
        """Yield assets, optionally only those changed since `updated_after`
        (the incremental sync cursor). Implementations stream/paginate."""
        ...

    def asset_uri(self, asset_id: str) -> str:
        """The canonical Immich URI for an asset (`media[]` / `resource`)."""
        ...


def _exif_when(exif: dict[str, Any], asset: dict[str, Any]) -> str:
    return str(
        exif.get("dateTimeOriginal")
        or asset.get("localDateTime")
        or asset.get("fileCreatedAt")
        or ""
    )


def _people(asset: dict[str, Any]) -> list[ImmichPerson]:
    out: list[ImmichPerson] = []
    for p in asset.get("people") or []:
        pid = str(p.get("id") or "")
        name = str(p.get("name") or "").strip()
        # Immich surfaces unnamed face clusters too; only ingest named people —
        # an unnamed cluster has no person concept to write.
        if pid and name:
            out.append(ImmichPerson(id=pid, name=name))
    return out


class RestImmichClient:
    """Thin aiohttp wrapper over the Immich REST API (read-only).

    `shared_resolver` maps an Immich asset's owner/share metadata to resident
    uids; left default, every asset is owner-scoped (the adapter falls back to
    the ingesting resident). Sharing is an Immich fact (§6), so the box wires a
    concrete resolver — the adapter stays source-agnostic.
    """

    _PAGE_SIZE = 250

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout: float = 30.0,
        shared_resolver: Any = None,
    ):
        self._base_url = base_url.rstrip("/")
        self._headers = {"x-api-key": api_key, "Accept": "application/json"}
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._shared_resolver = shared_resolver

    def asset_uri(self, asset_id: str) -> str:
        return f"{self._base_url}/api/assets/{asset_id}"

    async def iter_assets(
        self, *, updated_after: str = ""
    ) -> AsyncIterator[ImmichAsset]:
        page = 1
        async with aiohttp.ClientSession(timeout=self._timeout) as client:
            while True:
                body: dict[str, Any] = {"page": page, "size": self._PAGE_SIZE}
                if updated_after:
                    body["updatedAfter"] = updated_after
                payload = await self._fetch_page(client, body, page)
                items = (payload.get("assets") or {}).get("items") or []
                for raw in items:
                    yield self._asset(raw)
                next_page = (payload.get("assets") or {}).get("nextPage")
                if not next_page:
                    return
                page = int(next_page)

    async def _fetch_page(
        self, client: aiohttp.ClientSession, body: dict[str, Any], page: int
    ) -> dict[str, Any]:
        """POST one search page, retrying the SAME page on a transient transport
        failure (keep-alive drop / timeout) before giving up (#597)."""
        last_exc: Exception | None = None
        for attempt in range(len(_PAGE_RETRY_BACKOFF) + 1):
            try:
                async with client.post(
                    f"{self._base_url}/api/search/metadata",
                    json=body,
                    headers=self._headers,
                ) as resp:
                    resp.raise_for_status()
                    return await resp.json()
            except (aiohttp.ClientError, TimeoutError) as e:
                last_exc = e
                if attempt < len(_PAGE_RETRY_BACKOFF):
                    log.info(
                        "engine.ingest.immich_page_retry",
                        page=page,
                        attempt=attempt + 1,
                        error=str(e),
                    )
                    await asyncio.sleep(_PAGE_RETRY_BACKOFF[attempt])
        assert last_exc is not None
        raise last_exc

    def _asset(self, raw: dict[str, Any]) -> ImmichAsset:
        exif = raw.get("exifInfo") or {}
        lat = exif.get("latitude")
        lon = exif.get("longitude")
        shared = self._shared_resolver(raw) if self._shared_resolver else []
        return ImmichAsset(
            id=str(raw.get("id") or ""),
            file_name=str(raw.get("originalFileName") or ""),
            when=_exif_when(exif, raw),
            checksum=str(raw.get("checksum") or raw.get("id") or ""),
            latitude=float(lat) if lat is not None else None,
            longitude=float(lon) if lon is not None else None,
            city=str(exif.get("city") or ""),
            state=str(exif.get("state") or ""),
            country=str(exif.get("country") or ""),
            people=_people(raw),
            shared_with=list(shared),
        )

"""Jellyfin music ingest adapter (#564 slice 1, docs/okf-write-contract.md §6).

Reads the household Jellyfin **music** catalog read-only and writes OKF concepts
via the shared #447 writer:

  - **band** — one per Jellyfin MusicArtist (and per AlbumArtist string seen on a
    track), the artist the songs link to;
  - **song** — one per Audio track: `title`, `artist`, `genre`, `year`,
    `resource` = `jellyfin://audio/<id>`, plus a `by → [[bands/…]]` relationship
    to its artist.

So "welche Musik von <artist> habe ich" resolves from the central knowledge
store and music becomes a research source. Films/audiobooks + playback are out
of scope (slices 2/3).

Per-library ownership (#576): the adapter enumerates the libraries
(`GET /Library/MediaFolders`) and maps each library NAME to an owner uid (the
`JELLYFIN_LIBRARY_OWNERS` config; default 'Music (cdopp)' -> cdopp, everything
else -> household). A private library's concepts are written under the owner's
path (`users/<owner>/okf/...`); shared libraries stay household. A band that
appears in any shared library stays shared (shared artists stay shared).

Auth: the engine reuses the existing JELLYFIN_USERNAME/JELLYFIN_PASSWORD stack
vars (not an API key) — `POST /Users/AuthenticateByName` with an
`X-Emby-Authorization` header yields an AccessToken, then the music API is
called with `X-Emby-Token`.

Idempotent: every write goes through the writer's `ingest_log`
(`source="jellyfin"`, the item id) + `content_hash`, so a re-run with unchanged
metadata is a no-op. The run reports a high-water `cursor` (max item
`DateLastMediaAdded`/`DateCreated`) the caller persists for the next run.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Protocol

import aiohttp

from ...logging import log
from ..knowledge import ConceptRecord, Relationship, safe_slug
from ..knowledge.writer import OkfWriter


_SOURCE = "jellyfin"


@dataclass(frozen=True)
class JellyfinItem:
    """The normalized subset of a Jellyfin music item the adapter ingests.

    `kind` is the Jellyfin item type (`MusicArtist` or `Audio`); a track carries
    its `artist`/`album`/`genre`/`year`. `changed` is the item's change key
    (DateLastMediaAdded/DateCreated) used both as the high-water cursor and as
    the content-hash change input.
    """

    id: str
    kind: str
    name: str
    artist: str = ""
    album: str = ""
    genre: str = ""
    year: str = ""
    changed: str = ""


class JellyfinMusicClient(Protocol):
    """Read-only Jellyfin music access the adapter needs. Injectable for tests."""

    async def authenticate(self) -> None:
        """Exchange username/password for an access token. Idempotent."""
        ...

    async def libraries(self) -> list[tuple[str, str]]:
        """The media libraries as `(id, name)` — `GET /Library/MediaFolders`.
        The adapter maps a library NAME to an owner uid (#576 per-library)."""
        ...

    def iter_library(self, library_id: str) -> AsyncIterator[JellyfinItem]:
        """Yield one library's music items (artists + tracks). Implementations
        paginate."""
        ...

    def audio_uri(self, item_id: str) -> str:
        """The canonical Jellyfin URI for an audio track (`resource`)."""
        ...


def _str(d: dict[str, Any], key: str) -> str:
    return str(d.get(key) or "").strip()


def _first(d: dict[str, Any], key: str) -> str:
    vals = d.get(key) or []
    return str(vals[0]).strip() if vals else ""


class RestJellyfinMusicClient:
    """Thin aiohttp wrapper over the Jellyfin REST API (read-only).

    Authenticates with username/password (not an API key); the music API is
    then called with the `X-Emby-Token` access token. Only `GET`/auth `POST`.
    """

    _PAGE_SIZE = 500
    _AUTH_HEADER = (
        'MediaBrowser Client="Solaris", Device="Solaris",'
        ' DeviceId="solaris-ingest", Version="1"'
    )

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        *,
        timeout: float = 30.0,
    ):
        self._base_url = base_url.rstrip("/")
        self._username = username
        self._password = password
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._token = ""
        self._user_id = ""

    def audio_uri(self, item_id: str) -> str:
        return f"jellyfin://audio/{item_id}"

    async def authenticate(self) -> None:
        if self._token:
            return
        async with aiohttp.ClientSession(timeout=self._timeout) as client:
            async with client.post(
                f"{self._base_url}/Users/AuthenticateByName",
                json={"Username": self._username, "Pw": self._password},
                headers={"X-Emby-Authorization": self._AUTH_HEADER},
            ) as resp:
                resp.raise_for_status()
                payload = await resp.json()
        self._token = str(payload.get("AccessToken") or "")
        self._user_id = str((payload.get("User") or {}).get("Id") or "")

    async def libraries(self) -> list[tuple[str, str]]:
        await self.authenticate()
        headers = {"X-Emby-Token": self._token, "Accept": "application/json"}
        async with aiohttp.ClientSession(timeout=self._timeout) as client:
            async with client.get(
                f"{self._base_url}/Library/MediaFolders", headers=headers
            ) as resp:
                resp.raise_for_status()
                payload = await resp.json()
        return [
            (_str(f, "Id"), _str(f, "Name"))
            for f in (payload.get("Items") or [])
            if _str(f, "Id")
        ]

    async def iter_library(self, library_id: str) -> AsyncIterator[JellyfinItem]:
        await self.authenticate()
        headers = {"X-Emby-Token": self._token, "Accept": "application/json"}
        start = 0
        async with aiohttp.ClientSession(timeout=self._timeout) as client:
            while True:
                params = {
                    "ParentId": library_id,
                    "IncludeItemTypes": "MusicArtist,Audio",
                    "Recursive": "true",
                    "Fields": "Genres,AlbumArtist,ProductionYear,DateCreated",
                    "StartIndex": str(start),
                    "Limit": str(self._PAGE_SIZE),
                }
                async with client.get(
                    f"{self._base_url}/Items",
                    params=params,
                    headers=headers,
                ) as resp:
                    resp.raise_for_status()
                    payload = await resp.json()
                items = payload.get("Items") or []
                for raw in items:
                    yield self._item(raw)
                start += len(items)
                total = int(payload.get("TotalRecordCount") or 0)
                if not items or start >= total:
                    return

    def _item(self, raw: dict[str, Any]) -> JellyfinItem:
        year = raw.get("ProductionYear")
        return JellyfinItem(
            id=_str(raw, "Id"),
            kind=_str(raw, "Type"),
            name=_str(raw, "Name"),
            artist=_str(raw, "AlbumArtist") or _first(raw, "Artists"),
            album=_str(raw, "Album"),
            genre=_first(raw, "Genres"),
            year=str(year) if year is not None else "",
            changed=_str(raw, "DateLastMediaAdded") or _str(raw, "DateCreated"),
        )


@dataclass
class JellyfinIngestStats:
    items: int = 0
    bands_written: int = 0
    songs_written: int = 0
    skipped: int = 0
    # High-water sync cursor (max item `changed`) for the next incremental run.
    cursor: str = ""


_SHARED = "household"


class JellyfinMusicIngest:
    def __init__(
        self,
        client: JellyfinMusicClient,
        writer: OkfWriter,
        *,
        ingesting_uid: str,
        library_owners: dict[str, str] | None = None,
    ):
        self._client = client
        self._writer = writer
        self._uid = ingesting_uid
        # Library NAME -> owner uid (#576 per-library ownership). A library not
        # in the map is shared (household). Default 'Music (cdopp)' -> cdopp is
        # supplied by the runner from JELLYFIN_LIBRARY_OWNERS config.
        self._library_owners = {
            name.casefold(): uid for name, uid in (library_owners or {}).items()
        }
        # Bands written this run, so a track's artist isn't re-written per track.
        self._seen_bands: set[str] = set()
        # Bands seen in a SHARED library: a band there stays household even if it
        # also appears in a private library (shared artists stay shared).
        self._shared_bands: set[str] = set()

    def _owner_for(self, library_name: str) -> str:
        return self._library_owners.get(library_name.casefold(), _SHARED)

    async def run(self) -> JellyfinIngestStats:
        """Ingest the music catalog per library; return stats + high-water cursor.

        Each library maps to an owner: a shared library writes household-scoped
        concepts (vault `okf/...`), a private library ('Music (cdopp)') writes
        the owner's concepts (`users/<owner>/okf/...`). A two-pass walk so a band
        in any shared library stays shared even if also in a private one.
        """
        stats = JellyfinIngestStats()
        await self._client.authenticate()
        libraries = await self._client.libraries()
        owned = [(lib_id, self._owner_for(name)) for lib_id, name in libraries]
        # Shared libraries first so a shared band claims the household scope
        # before a private library would route it under a user path.
        owned.sort(key=lambda lo: lo[1] != _SHARED)
        for lib_id, owner in owned:
            async for item in self._client.iter_library(lib_id):
                try:
                    self._ingest_item(item, owner, stats)
                except Exception as e:  # noqa: BLE001
                    # One bad item (e.g. a name that fails safe_slug) must never
                    # abort the whole run (mirrors Immich #528).
                    log.error(
                        "engine.ingest.jellyfin_item_failed",
                        item_id=item.id,
                        error=str(e),
                    )
                    stats.skipped += 1
                else:
                    if item.changed > stats.cursor:
                        stats.cursor = item.changed
                stats.items += 1
        return stats

    def _ingest_item(
        self, item: JellyfinItem, owner: str, stats: JellyfinIngestStats
    ) -> None:
        if item.kind == "MusicArtist":
            self._write_band(item.name, owner, stats)
        elif item.kind == "Audio":
            rels: list[Relationship] = []
            if item.artist:
                self._write_band(item.artist, owner, stats)
                rels.append(Relationship("by", f"bands/{safe_slug(item.artist)}"))
            self._write_song(item, owner, rels, stats)

    def _write_band(self, name: str, owner: str, stats: JellyfinIngestStats) -> None:
        if not name:
            return
        slug = safe_slug(name)
        # Shared wins: once a band is shared it never gets re-scoped to a user.
        if owner == _SHARED:
            self._shared_bands.add(slug)
        elif slug in self._shared_bands:
            owner = _SHARED
        if slug in self._seen_bands:
            return
        self._seen_bands.add(slug)
        rec = ConceptRecord(
            type="band",
            title=name,
            source=_SOURCE,
            external_id=f"artist/{slug}",
            resident=owner,
            resource=f"jellyfin://artist/{slug}",
        )
        if not self._writer.write_concept(rec, ingesting_uid=self._uid).skipped:
            stats.bands_written += 1

    def _write_song(
        self,
        item: JellyfinItem,
        owner: str,
        rels: list[Relationship],
        stats: JellyfinIngestStats,
    ) -> None:
        uri = self._client.audio_uri(item.id)
        extra: dict[str, object] = {}
        if item.artist:
            extra["artist"] = item.artist
            extra["by"] = f"bands/{safe_slug(item.artist)}"
        if item.album:
            extra["album"] = item.album
        if item.genre:
            extra["genre"] = item.genre
        if item.year:
            extra["year"] = item.year
        rec = ConceptRecord(
            type="song",
            title=item.name or f"Track {item.id}",
            # Suffix the item id so two tracks with the same title don't collide
            # on the title-derived slug; re-ingest of the same track is stable.
            slug=f"{safe_slug(item.name or item.id)}-{safe_slug(item.id)}",
            source=_SOURCE,
            external_id=f"audio/{item.id}",
            resident=owner,
            resource=uri,
            # Metadata rides the body so a changed track moves the content_hash
            # and re-ingests.
            body=(
                f"Jellyfin track {item.id}"
                f" (artist {item.artist}, album {item.album},"
                f" genre {item.genre}, year {item.year})."
            ),
            extra=extra,
            relationships=rels,
        )
        if self._writer.write_concept(rec, ingesting_uid=self._uid).skipped:
            stats.skipped += 1
        else:
            stats.songs_written += 1

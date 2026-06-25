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

Per-library ownership (#576): the adapter enumerates the music libraries
(`GET /Users/{userId}/Views`, user-scoped so the read-only service user can read
them — #581) and maps each library NAME to an owner uid (the
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
    # MusicArtist enrichment: genres (joined) + the bio Overview (#592).
    genres: str = ""
    overview: str = ""


class JellyfinMusicClient(Protocol):
    """Read-only Jellyfin music access the adapter needs. Injectable for tests."""

    async def authenticate(self) -> None:
        """Exchange username/password for an access token. Idempotent."""
        ...

    async def libraries(self) -> list[tuple[str, str]]:
        """The music media libraries as `(id, name)` — `GET /Users/{userId}/Views`.
        The adapter maps a library NAME to an owner uid (#576 per-library)."""
        ...

    def iter_library(self, library_id: str) -> AsyncIterator[JellyfinItem]:
        """Yield one library's music items (artists + tracks). Implementations
        paginate."""
        ...

    def audio_uri(self, item_id: str) -> str:
        """The canonical Jellyfin URI for an audio track (`resource`)."""
        ...

    async def lyrics(self, audio_id: str) -> str | None:
        """The track's lyrics as one joined string, or ``None`` when the track
        has none (404/empty). Fetched live — no bulk ingest (#593)."""
        ...


def _str(d: dict[str, Any], key: str) -> str:
    return str(d.get(key) or "").strip()


def _first(d: dict[str, Any], key: str) -> str:
    vals = d.get(key) or []
    return str(vals[0]).strip() if vals else ""


def _join(d: dict[str, Any], key: str) -> str:
    vals = d.get(key) or []
    return ", ".join(str(v).strip() for v in vals if str(v).strip())


def _lyric_text(payload: Any) -> str | None:
    """Join a Jellyfin LyricResponse into one plain-text string, or ``None``.

    The endpoint returns lyric *lines* under `Lyrics` — a list of
    `{"Text": "...", "Start": ...}` (timed) — but the shape varies by
    deployment: the list may sit one level deeper (`Lyrics.Lyrics`), the lines
    may be bare strings, or the whole body may be a plain string. Pull out the
    `Text` of each line (or the bare string), drop empties, and join with
    newlines; return ``None`` when nothing usable is present."""
    if isinstance(payload, str):
        text = payload.strip()
        return text or None
    if isinstance(payload, dict):
        lines = payload.get("Lyrics")
        if isinstance(lines, dict):
            lines = lines.get("Lyrics")
        if isinstance(lines, list):
            parts: list[str] = []
            for line in lines:
                if isinstance(line, dict):
                    parts.append(str(line.get("Text") or "").strip())
                else:
                    parts.append(str(line).strip())
            text = "\n".join(p for p in parts if p)
            return text or None
    return None


class RestJellyfinMusicClient:
    """Thin aiohttp wrapper over the Jellyfin REST API (read-only).

    Authenticates with username/password (not an API key); the music API is
    then called with the `X-Emby-Token` access token. Only `GET`/auth `POST`.
    """

    _PAGE_SIZE = 500
    # A long ingest can outlive one session token; cap how many times the whole
    # run will re-authenticate so a server that 401s every request can't loop.
    _MAX_REAUTH = 5
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
        self._reauths = 0

    def audio_uri(self, item_id: str) -> str:
        return f"jellyfin://audio/{item_id}"

    def _auth_headers(self) -> dict[str, str]:
        return {"X-Emby-Token": self._token, "Accept": "application/json"}

    async def authenticate(self) -> None:
        if self._token:
            return
        await self._reauthenticate()

    async def _reauthenticate(self) -> None:
        # Force a fresh AuthenticateByName even if a (stale) token is held, so a
        # 401 mid-ingest can recover; bounded by _MAX_REAUTH across the run.
        self._token = ""
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

    async def _get_json(
        self, client: aiohttp.ClientSession, path: str, *, params: dict | None = None
    ) -> dict[str, Any]:
        """Authenticated GET that survives a mid-ingest token expiry: on a 401 it
        re-authenticates (fresh token + userId) and retries the SAME request from
        the same StartIndex, so pagination resumes with the new token instead of
        truncating the tail. A long ingest can outlive several tokens, so it
        re-auths on every 401 up to `_MAX_REAUTH` total across the run.

        Jellyfin's 401 can surface two ways depending on the deployment/proxy: as
        a returned `resp.status == 401`, or — when raise-on-status is in effect
        somewhere in the client/proxy chain — as a raised
        `aiohttp.ClientResponseError(status=401)`. u78 only handled the first and
        never fired on the real box (#583); handle both."""
        url = f"{self._base_url}{path}"
        while True:
            try:
                async with client.get(
                    url, params=params, headers=self._auth_headers()
                ) as resp:
                    if resp.status == 401:
                        raise aiohttp.ClientResponseError(
                            resp.request_info, resp.history, status=401
                        )
                    resp.raise_for_status()
                    return await resp.json()
            except aiohttp.ClientResponseError as e:
                if e.status != 401 or self._reauths >= self._MAX_REAUTH:
                    raise
                self._reauths += 1
                log.info("engine.ingest.jellyfin_reauth", path=path)
                await self._reauthenticate()

    async def lyrics(self, audio_id: str) -> str | None:
        """Fetch a track's lyrics live (#593): `GET /Audio/{id}/Lyrics`, authed
        as the read-only service user with the same re-auth-on-401 path as the
        ingest GETs. A track with no lyrics 404s → ``None``; any other transport
        error degrades to ``None`` so a query never crashes on a missing track."""
        await self.authenticate()
        async with aiohttp.ClientSession(timeout=self._timeout) as client:
            try:
                payload = await self._get_json(client, f"/Audio/{audio_id}/Lyrics")
            except aiohttp.ClientResponseError as e:
                if e.status == 404:
                    return None
                raise
        return _lyric_text(payload)

    async def libraries(self) -> list[tuple[str, str]]:
        # User-scoped Views (not admin-only /Library/MediaFolders, which 403s for
        # the read-only service user, #581); keep only music collections so
        # Playlists/non-music are excluded.
        await self.authenticate()
        async with aiohttp.ClientSession(timeout=self._timeout) as client:
            payload = await self._get_json(client, f"/Users/{self._user_id}/Views")
        return [
            (_str(f, "Id"), _str(f, "Name"))
            for f in (payload.get("Items") or [])
            if _str(f, "Id") and _str(f, "CollectionType").casefold() == "music"
        ]

    async def iter_library(self, library_id: str) -> AsyncIterator[JellyfinItem]:
        await self.authenticate()
        start = 0
        async with aiohttp.ClientSession(timeout=self._timeout) as client:
            while True:
                params = {
                    "ParentId": library_id,
                    "IncludeItemTypes": "MusicArtist,Audio",
                    "Recursive": "true",
                    "Fields": "Genres,AlbumArtist,ProductionYear,DateCreated,Overview",
                    "StartIndex": str(start),
                    "Limit": str(self._PAGE_SIZE),
                }
                # _get_json re-auths + retries this same page on a 401, so the
                # pagination resumes from `start` after a token expiry.
                payload = await self._get_json(client, "/Items", params=params)
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
            genres=_join(raw, "Genres"),
            overview=_str(raw, "Overview"),
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


def _slug_or_id(name: str, item_id: str) -> str:
    """An OKF-safe slug for `name`, falling back to the Jellyfin item id when the
    name slugifies empty (#583), so an unusual-name item is still captured rather
    than lost to a ValueError."""
    try:
        return safe_slug(name)
    except ValueError:
        return safe_slug(f"item-{item_id}")


def _band_facts(item: JellyfinItem) -> list[tuple[str, str]]:
    """The (predicate, value) facts a MusicArtist carries: genre + bio (#592)."""
    facts: list[tuple[str, str]] = []
    if item.genres:
        facts.append(("genre", item.genres))
    if item.overview:
        facts.append(("bio", item.overview))
    return facts


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
        # Bands written WITH their genre/bio facts (#592), so a later bare
        # track-write doesn't clobber the enrichment.
        self._enriched_bands: set[str] = set()
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
            self._write_band(item.name, item.id, owner, stats, facts=_band_facts(item))
        elif item.kind == "Audio":
            rels: list[Relationship] = []
            if item.artist:
                # Fall back to this track's id only when the artist name has no
                # own slug, so the band concept and the `by` edge share a slug.
                slug = self._write_band(item.artist, item.id, owner, stats)
                rels.append(Relationship("by", f"bands/{slug}"))
            self._write_song(item, owner, rels, stats)

    def _write_band(
        self,
        name: str,
        item_id: str,
        owner: str,
        stats: JellyfinIngestStats,
        *,
        facts: list[tuple[str, str]] | None = None,
    ) -> str:
        slug = _slug_or_id(name, item_id)
        # Shared wins: once a band is shared it never gets re-scoped to a user.
        if owner == _SHARED:
            self._shared_bands.add(slug)
        elif slug in self._shared_bands:
            owner = _SHARED
        # A bare (track-derived) write doesn't block the later enriched
        # MusicArtist write; once enriched, re-writes are short-circuited. So
        # genre/bio land regardless of artist-vs-track iteration order.
        enrich = facts or []
        if slug in self._seen_bands and (not enrich or slug in self._enriched_bands):
            return slug
        self._seen_bands.add(slug)
        if enrich:
            self._enriched_bands.add(slug)
        rec = ConceptRecord(
            type="band",
            title=name,
            # Pin the slug (id-based when the name slugifies empty, #583) so the
            # okf path matches the `by` edge and an unusual name isn't lost.
            slug=slug,
            source=_SOURCE,
            external_id=f"artist/{slug}",
            resident=owner,
            resource=f"jellyfin://artist/{slug}",
            facts=enrich,
        )
        if not self._writer.write_concept(rec, ingesting_uid=self._uid).skipped:
            stats.bands_written += 1
        return slug

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
            extra["by"] = f"bands/{_slug_or_id(item.artist, item.id)}"
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
            # Both halves go through the id-fallback so no slug source can raise
            # at the throw site (#583).
            slug=f"{_slug_or_id(item.name, item.id)}-{_slug_or_id(item.id, item.id)}",
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
            # The Jellyfin URI as a scoped fact so on-demand lyrics (#593) can
            # resolve a song → its audio id from the per-owner facts table,
            # without re-reading the OKF file or leaking the id elsewhere.
            facts=[("resource", uri)],
            relationships=rels,
        )
        if self._writer.write_concept(rec, ingesting_uid=self._uid).skipped:
            stats.skipped += 1
        else:
            stats.songs_written += 1

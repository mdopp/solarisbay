"""Radio tool — play a resident's favorite station, ask + store it if unknown.

`play_radio` resolves a station NAME to a playable stream via radio-browser.info
(a free, keyless community index) and casts it through the same scoped
`media_player.play_media` path play_music uses (#604), so the engine never holds
a client-side HA token. The favorite is a deterministic per-user note at
`users/<uid>/preferences/radio-favorit.md` (frontmatter name + stream_url) — read
and written only for the calling resident, never another's (#576). "Spiele Radio"
with no station plays the stored favorite; an unknown favorite returns
`no_favorite` so the model can ask and call back with `station=<answer>`, which
stores it and plays.

Defensive by design: a radio-browser shape mismatch or an unreachable mirror
returns None (no match) rather than crashing — the real-box /verify confirms live
reachability.
"""

from __future__ import annotations

import json
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote

import aiohttp

from solaris_chat.engine.tools import Tool
from solaris_chat.engine.tools.ha import call_service_scoped

_MIRROR = "https://de1.api.radio-browser.info"
_TIMEOUT = aiohttp.ClientTimeout(total=20)


def _user_agent() -> str:
    try:
        return f"Solaris/{version('solaris-chat')}"
    except PackageNotFoundError:
        return "Solaris/0"


class StationResolver(Protocol):
    async def resolve_station(self, name: str) -> tuple[str, str] | None: ...


class RadioBrowserClient:
    """Resolve a station name to (canonical_name, stream_url) via radio-browser.info."""

    def __init__(self, mirror: str = _MIRROR) -> None:
        self._mirror = mirror.rstrip("/")

    async def resolve_station(self, name: str) -> tuple[str, str] | None:
        query = name.strip()
        if not query:
            return None
        url = (
            f"{self._mirror}/json/stations/search"
            f"?name={quote(query)}&order=votes&reverse=true&limit=5"
        )
        headers = {"User-Agent": _user_agent()}
        try:
            async with aiohttp.ClientSession(timeout=_TIMEOUT) as client:
                async with client.get(url, headers=headers) as resp:
                    if resp.status >= 400:
                        return None
                    stations = await resp.json()
        except (aiohttp.ClientError, TimeoutError, ValueError):
            return None
        if not isinstance(stations, list):
            return None
        playable = [
            s
            for s in stations
            if isinstance(s, dict) and str(s.get("url_resolved") or "").strip()
        ]
        if not playable:
            return None
        best = max(playable, key=lambda s: s.get("votes") or 0)
        return str(best.get("name") or query), str(best["url_resolved"]).strip()


def _favorite_path(notes_dir: str, uid: str) -> Path:
    return Path(notes_dir) / "users" / uid / "preferences" / "radio-favorit.md"


def _read_favorite(notes_dir: str, uid: str) -> dict[str, str] | None:
    path = _favorite_path(notes_dir, uid)
    if not path.is_file():
        return None
    fields: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip() in ("name", "stream_url"):
            fields[key.strip()] = value.strip()
    if "name" in fields and "stream_url" in fields:
        return {"name": fields["name"], "stream_url": fields["stream_url"]}
    return None


def _write_favorite(notes_dir: str, uid: str, name: str, url: str) -> None:
    path = _favorite_path(notes_dir, uid)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"---\nname: {name}\nstream_url: {url}\n---\n",
        encoding="utf-8",
    )


def build_radio_tools(
    notes_dir: str, hass_url: str, hass_token: str, uid_getter
) -> list[Tool]:
    resolver: StationResolver = RadioBrowserClient()

    async def play_radio(args: dict[str, Any]) -> str:
        entity_id = str(args.get("entity_id") or "").strip()
        station = str(args.get("station") or "").strip()
        if not entity_id:
            return json.dumps({"ok": False, "reason": "need_device"})
        caller = uid_getter()

        if station:
            resolved = await resolver.resolve_station(station)
            if resolved is None:
                return json.dumps(
                    {"ok": False, "reason": "station_not_found", "query": station},
                    ensure_ascii=False,
                )
            name, url = resolved
            _write_favorite(notes_dir, caller, name, url)
        else:
            fav = _read_favorite(notes_dir, caller)
            if fav is None:
                return json.dumps({"ok": False, "reason": "no_favorite"})
            name, url = fav["name"], fav["stream_url"]

        result = await call_service_scoped(
            hass_url,
            hass_token,
            entity_id,
            "media_player.play_media",
            {"media_content_type": "music", "media_content_id": url},
        )
        if not result.get("ok"):
            return json.dumps(
                {
                    "ok": False,
                    "reason": "play_failed",
                    "detail": result.get("error"),
                    "station": name,
                },
                ensure_ascii=False,
            )
        return json.dumps(
            {
                "ok": True,
                "station": name,
                "entity_id": entity_id,
                "played": True,
            },
            ensure_ascii=False,
        )

    return [
        Tool(
            name="play_radio",
            description=(
                "Spielt den Lieblings-Radiosender des Bewohners auf einem"
                " Raum-Gerät. Für 'Spiele Radio' rufst du es OHNE 'station' auf —"
                " dann wird der gespeicherte Lieblingssender gespielt. Liefert es"
                " reason:no_favorite, frag den Nutzer nach seinem Lieblingssender"
                " und ruf erneut mit 'station=<Antwort>' auf — das löst den Sender"
                " über radio-browser.info auf, SPEICHERT ihn dauerhaft als"
                " Lieblingssender und spielt ihn. Übergib die media_player-Entität"
                " des Zielraums als 'entity_id' (z.B. media_player.wohnzimmer);"
                " ohne entity_id kommt reason:need_device — frag dann nach dem"
                " Gerät. Bestätige nur den zurückgegebenen Sendernamen, erfinde"
                " keinen. NUR für Radiosender — NICHT für Musik (play_music) oder"
                " Podcasts (media_find_podcast)."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "station": {"type": "string"},
                    "entity_id": {"type": "string"},
                },
            },
            handler=play_radio,
        ),
    ]

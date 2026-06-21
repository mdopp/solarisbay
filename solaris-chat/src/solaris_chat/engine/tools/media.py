"""Podcast tools — find a show on fyyd.de and play its newest episode.

fyyd.de is a free, keyless, open podcast index (no key, no account, no id). The
search endpoint resolves a show by name; its `xmlURL` is the RSS feed whose
newest `<enclosure>` is the episode audio. Played through the same scoped
`media_player.play_media` path the card actions use (#476/#511/#512) so the
engine never holds a client-side HA token.

Defensive by design: a fyyd/feed shape mismatch or an unreachable index fails
gracefully ("not found") rather than crashing — the real-box /verify (with
internet) confirms live reachability.
"""

from __future__ import annotations

import json
from email.utils import parsedate_to_datetime
from typing import Any
from xml.etree import ElementTree

import aiohttp

from solaris_chat.engine.tools import Tool
from solaris_chat.engine.tools.ha import call_service_scoped

_FYYD_SEARCH = "https://api.fyyd.de/0.2/search/podcast"
_TIMEOUT = aiohttp.ClientTimeout(total=20)


async def _fyyd_resolve_feed(name: str) -> dict[str, Any] | None:
    """Resolve a show name to its {title, feed} via fyyd's keyless search."""
    async with aiohttp.ClientSession(timeout=_TIMEOUT) as client:
        async with client.get(_FYYD_SEARCH, params={"title": name, "count": 1}) as resp:
            if resp.status >= 400:
                return None
            body = await resp.json()
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, list) or not data:
        return None
    show = data[0]
    feed = str(show.get("xmlURL") or "").strip()
    if not feed.startswith("http"):
        return None
    return {"title": str(show.get("title") or name), "feed": feed}


def _newest_enclosure(feed_xml: str) -> dict[str, str] | None:
    """Newest item's enclosure URL + title from an RSS feed.

    Items are usually newest-first, but pick by `pubDate` so a misordered feed
    still yields the latest episode.
    """
    root = ElementTree.fromstring(feed_xml)
    items = root.findall(".//item")
    best: tuple[float, dict[str, str]] | None = None
    for idx, item in enumerate(items):
        enclosure = item.find("enclosure")
        url = enclosure.get("url") if enclosure is not None else None
        if not url:
            continue
        title_el = item.find("title")
        ep = {
            "title": (title_el.text or "").strip() if title_el is not None else "",
            "url": url.strip(),
        }
        when = item.find("pubDate")
        ts = -float(idx)  # document order fallback (earlier = newer)
        if when is not None and when.text:
            try:
                ts = parsedate_to_datetime(when.text).timestamp()
            except (TypeError, ValueError):
                pass
        if best is None or ts > best[0]:
            best = (ts, ep)
    return best[1] if best else None


def build_media_tools(hass_url: str, hass_token: str) -> list[Tool]:
    async def find_podcast(args: dict[str, Any]) -> str:
        name = str(args.get("name") or "").strip()
        if not name:
            return json.dumps({"ok": False, "reason": "missing_name"})
        entity_id = str(args.get("entity_id") or "").strip()

        show = await _fyyd_resolve_feed(name)
        if show is None:
            return json.dumps(
                {"ok": False, "reason": "show_not_found", "query": name},
                ensure_ascii=False,
            )
        async with aiohttp.ClientSession(timeout=_TIMEOUT) as client:
            async with client.get(show["feed"]) as resp:
                if resp.status >= 400:
                    return json.dumps(
                        {
                            "ok": False,
                            "reason": "feed_unavailable",
                            "show": show["title"],
                        },
                        ensure_ascii=False,
                    )
                feed_xml = await resp.text()
        try:
            episode = _newest_enclosure(feed_xml)
        except ElementTree.ParseError:
            episode = None
        if episode is None:
            return json.dumps(
                {"ok": False, "reason": "no_episode", "show": show["title"]},
                ensure_ascii=False,
            )

        if not entity_id:
            # No device given — hand the resolved episode back so the model can
            # ask which room, then call again with entity_id.
            return json.dumps(
                {
                    "ok": True,
                    "show": show["title"],
                    "episode": episode["title"],
                    "media_url": episode["url"],
                    "played": False,
                },
                ensure_ascii=False,
            )

        result = await call_service_scoped(
            hass_url,
            hass_token,
            entity_id,
            "media_player.play_media",
            {"media_content_type": "music", "media_content_id": episode["url"]},
        )
        if not result.get("ok"):
            return json.dumps(
                {
                    "ok": False,
                    "reason": "play_failed",
                    "detail": result.get("error"),
                    "show": show["title"],
                    "episode": episode["title"],
                },
                ensure_ascii=False,
            )
        return json.dumps(
            {
                "ok": True,
                "show": show["title"],
                "episode": episode["title"],
                "media_url": episode["url"],
                "entity_id": entity_id,
                "played": True,
            },
            ensure_ascii=False,
        )

    return [
        Tool(
            name="media_find_podcast",
            description=(
                "Findet einen Podcast über den freien Index fyyd.de und spielt die"
                " neueste Folge auf einem Raum-Gerät. Übergib den Podcast-Namen als"
                " 'name' und die media_player-Entität des Zielraums als 'entity_id'"
                " (z.B. media_player.wohnzimmer). Ohne entity_id wird nur die"
                " neueste Folge aufgelöst (frag dann nach dem Raum und ruf erneut)."
                " Nutze es für 'Spiel die neueste Folge von <Podcast>'."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "entity_id": {"type": "string"},
                },
                "required": ["name"],
            },
            handler=find_podcast,
        ),
    ]

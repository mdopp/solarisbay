"""Turn a YouTube Music listening history into a shopping list of albums the
user does NOT yet have in their library.

Pipeline:
  1. Parse ``watch-history.json`` — keep only ``header == "YouTube Music"``
     entries; extract artist/title/videoId and count plays.
  2. Subtract what the library already owns (``library.owned_keys``).
  3. Resolve each missing track's album from YouTube Music itself via the exact
     videoId (``ytmusicapi``), cached on disk. Unresolved tracks are grouped
     under "(unbekannt)" per artist — never silently dropped.
  4. Aggregate per album: distinct heard tracks + summed play counts, sorted by
     total plays. Exportable as CSV or Markdown.

Data paths (the on-disk album cache, the scanned library) are injected via
``ImporterPaths`` rather than read from a module-level config.
"""

from __future__ import annotations

import csv
import io
import json
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

from . import catalog, library
from .paths import ImporterPaths
from .textnorm import normalize, track_key

# Trailing video-title noise on non-Topic uploads: "(Official Video)", "[Lyrics]"…
_VIDEO_TAG = re.compile(
    r"\s*[\(\[][^\)\]]*(official|lyric|audio|visualizer|music\s*video|\bmv\b|hd|4k|remaster)[^\)\]]*[\)\]]",
    re.IGNORECASE,
)
_VARIOUS = {
    "various artists",
    "various",
    "va",
    "verschiedene interpreten",
    "diverse",
    "compilation",
}


def _is_various(name: str | None) -> bool:
    return normalize(name or "") in {normalize(x) for x in _VARIOUS}


def _strip_video_tags(title: str) -> str:
    return _VIDEO_TAG.sub("", title).strip() or title


# Audiobook / audio-drama chapters: "Kapitel 12: …", "Folge 344", "Episode 3".
_HOERSPIEL = re.compile(r"\b(kapitel|folge|teil|episode)\s*\d+", re.IGNORECASE)


def _title_matches(a: str, b: str) -> bool:
    """Loose title equality used to VALIDATE a resolution: the album we accept
    must belong to the track we asked for (else get_watch_playlist's radio mix
    would tag unrelated songs with a bogus album)."""
    na, nb = normalize(a), normalize(b)
    return bool(na and nb and (na == nb or na in nb or nb in na))


# Resolve albums for at most this many missing tracks per run (most-played
# first). The rest still appear, grouped as unresolved — see UNRESOLVED_LABEL.
MAX_RESOLVE = 400
UNRESOLVED_LABEL = "(unbekannt / Single)"

_ANGESEHEN = " angesehen"
_WATCHED = "watched "


# ---------------------------------------------------------------------------
# History parsing
# ---------------------------------------------------------------------------


def _clean_title(raw: str) -> str:
    t = (raw or "").strip()
    if t.endswith(_ANGESEHEN):
        t = t[: -len(_ANGESEHEN)]
    elif t.lower().startswith(_WATCHED):
        t = t[len(_WATCHED) :]
    return t.strip()


def _clean_artist(raw: str) -> str:
    a = (raw or "").strip()
    if a.endswith(" - Topic"):
        a = a[: -len(" - Topic")]
    return a.strip()


def _video_id(url: str) -> str | None:
    try:
        qs = parse_qs(urlparse(url).query)
        return qs.get("v", [None])[0]
    except Exception:
        return None


def _entry_time(entry: dict) -> datetime | None:
    ts = entry.get("time")
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None


def _parse_track(entry: dict, subs: list) -> tuple[str, str, bool, bool, bool] | None:
    """Best-effort (artist, title, is_topic, is_hoerspiel) from a history entry.

    - "<Artist> - Topic" channels are YT Music art tracks → clean artist/title.
    - "Kapitel/Folge N …" titles are audiobook/audio-drama chapters (Hörspiel);
      we keep the channel as the artist and do NOT treat them as music.
    - other uploads usually title as "Artist - Song" while the channel is just an
      uploader, so split the title and drop "(Official Video)"-style tags.
    """
    channel = subs[0].get("name") or ""
    topic = channel.strip().endswith("- Topic")
    title = _clean_title(entry.get("title", ""))
    if not title:
        return None
    ch_clean = _clean_artist(channel)
    # Category signal from the shipped seed catalog (matched on the show name).
    seed = catalog.classify(ch_clean, title)
    hoerspiel = (
        bool(_HOERSPIEL.search(title))
        or "hörspiel" in title.lower()
        or "hörbuch" in title.lower()
    )
    podcast = False
    if seed == "Podcast":
        podcast, hoerspiel = True, False  # a known podcast wins over "Folge N"
    elif seed == "Hörspiel":
        hoerspiel = True
    if topic:
        artist = ch_clean
    elif hoerspiel or podcast:
        artist = ch_clean  # keep the show as the "artist"
    elif " - " in title:
        left, right = title.split(" - ", 1)
        artist, title = (
            left.strip(),
            (_strip_video_tags(right.strip()) or right.strip()),
        )
    else:
        artist, title = ch_clean, _strip_video_tags(title)
    return artist, title, topic, hoerspiel, podcast


def aggregate_plays(
    history_bytes: bytes, since: datetime | None = None
) -> dict[str, dict]:
    """Return videoId(or synthetic key) -> {artist, title, videoId, count, …}.

    ``since`` (if given) drops plays older than that timestamp.
    """
    data = json.loads(history_bytes)
    plays: dict[str, dict] = {}
    for entry in data:
        if entry.get("header") != "YouTube Music":
            continue
        if since is not None:
            t = _entry_time(entry)
            if t is not None and t < since:
                continue
        subs = entry.get("subtitles") or []
        if not subs:
            continue
        parsed = _parse_track(entry, subs)
        if parsed is None:
            continue
        artist, title, topic, hoerspiel, podcast = parsed
        vid = _video_id(entry.get("titleUrl", "")) or ""
        key = vid or track_key(artist, title)
        rec = plays.setdefault(
            key,
            {
                "artist": artist,
                "title": title,
                "videoId": vid,
                "count": 0,
                "topic": topic,
                "hoerspiel": hoerspiel,
                "podcast": podcast,
            },
        )
        rec["count"] += 1
        if topic:
            rec["topic"] = True
        if hoerspiel:
            rec["hoerspiel"] = True
        if podcast:
            rec["podcast"] = True
    return plays


# ---------------------------------------------------------------------------
# Album resolution (ytmusicapi, cached)
# ---------------------------------------------------------------------------


def _cache_path(data_dir):
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir / "ytmusic_album_cache.json"


def _load_cache(data_dir) -> dict:
    p = _cache_path(data_dir)
    if p.exists():
        try:
            return json.loads(p.read_text("utf-8"))
        except ValueError:
            return {}
    return {}


def _save_cache(data_dir, cache: dict) -> None:
    try:
        _cache_path(data_dir).write_text(json.dumps(cache), encoding="utf-8")
    except OSError:
        pass


_yt = None


def _yt_client():
    global _yt
    if _yt is None:
        from ytmusicapi import YTMusic

        _yt = YTMusic()  # unauthenticated — song lookups don't need auth
    return _yt


def _lookup_album(
    video_id: str, artist: str | None = None, title: str | None = None
) -> tuple[str | None, str | None]:
    """Resolve (album, album_artist) for a track — VALIDATED. get_watch_playlist
    returns a radio mix for non-music videos, so we only accept its album when the
    returned track's title matches ours. If that fails or the album is a "Various
    Artists" compilation, search by artist+title and accept only an
    artist+title-matching hit. We prefer *unresolved* over *wrong*."""
    album, alb_artist = None, None
    try:
        wp = _yt_client().get_watch_playlist(videoId=video_id, limit=1)
        track = (wp.get("tracks") or [{}])[0]
        if title and _title_matches(track.get("title", ""), title):
            album = (track.get("album") or {}).get("name")
            arts = track.get("artists") or []
            alb_artist = arts[0]["name"] if arts else None
    except Exception:
        pass
    if artist and title and (not album or _is_various(alb_artist)):
        a2, art2 = _search_album(artist, title)
        if a2:
            album, alb_artist = a2, art2
    return album, alb_artist


def _search_album(artist: str, title: str) -> tuple[str | None, str | None]:
    """Find the artist's own album for a song via search; accept only a result
    whose artist AND title match, so we never attach a wrong album."""
    try:
        results = _yt_client().search(f"{artist} {title}", filter="songs", limit=5)
    except Exception:
        return None, None
    want = normalize(artist)
    for r in results or []:
        alb = (r.get("album") or {}).get("name")
        arts = r.get("artists") or []
        if not alb or not arts or not _title_matches(r.get("title", ""), title):
            continue
        match = next(
            (a["name"] for a in arts if normalize(a.get("name", "")) == want), None
        )
        if match:
            return alb, match
    return None, None


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------


def analyze_iter(
    history_bytes: bytes,
    paths: ImporterPaths,
    *,
    min_plays: int = 1,
    months: int = 0,
    resolve: bool = True,
    cap: int | None = None,
    is_canceled=None,
):
    """Generator yielding progress events, terminating with one carrying the full
    ``result``. Upfront options bound the runtime (the caller collects them from
    the user before starting), so we don't spend minutes/hours on unwanted work:

    - ``min_plays``  — ignore tracks played fewer than this many times.
    - ``months``     — only consider plays from the last N months (0 = all).
    - ``resolve``    — resolve albums via YouTube Music (slow); if False, group by
      artist instantly.
    - ``cap``        — resolve at most this many missing tracks (most-played first).
    - ``is_canceled``— polled between items so a user cancel stops promptly.
    """
    is_canceled = is_canceled or (lambda: False)
    cap = MAX_RESOLVE if cap is None else cap
    since = None
    if months and months > 0:
        since = datetime.now(timezone.utc) - timedelta(days=30 * months)

    yield {"stage": "parse", "message": "Historie einlesen …", "pct": 2}
    plays = aggregate_plays(history_bytes, since=since)
    if min_plays and min_plays > 1:
        plays = {k: v for k, v in plays.items() if v["count"] >= min_plays}
    total_plays = sum(p["count"] for p in plays.values())
    scope = []
    if min_plays > 1:
        scope.append(f"≥{min_plays}×")
    if months:
        scope.append(f"letzte {months} Mon.")
    scope_txt = f" ({', '.join(scope)})" if scope else ""
    yield {
        "stage": "parse",
        "message": f"{len(plays)} Songs · {total_plays} Abspielungen{scope_txt}",
        "pct": 8,
    }

    # --- library scan (incremental; the slow, Jellyfin-side comparison) -------
    files = library.list_audio_files(paths)
    sig = library.signature_of(files)
    cached = library.cached_index(sig)
    if cached is None:
        owned, by_artist = set(), {}
        total = len(files)
        yield {
            "stage": "library",
            "message": f"Bibliothek scannen … 0/{total}",
            "done": 0,
            "total": total,
            "pct": 10,
        }
        for i, p in enumerate(files, 1):
            if is_canceled():
                return
            artist, title = library.tags(p, paths)
            library.add_owned(owned, by_artist, artist, title)
            if i % 200 == 0 or i == total:
                yield {
                    "stage": "library",
                    "message": f"Bibliothek scannen … {i}/{total}",
                    "done": i,
                    "total": total,
                    "pct": 10 + int(20 * i / max(total, 1)),
                }
        library.set_cache(sig, owned, by_artist, total)
    else:
        owned, by_artist = cached
        library.set_cache(sig, owned, by_artist, len(files))
        yield {
            "stage": "library",
            "message": f"Bibliothek gecacht ({len(files)} Tracks)",
            "pct": 30,
        }

    # --- diff (exact + fuzzy: catches library tag typos & "The" prefixes) -----
    missing = [
        p
        for p in plays.values()
        if not library.owns(owned, by_artist, p["artist"], p["title"])
    ]
    owned_matches = len(plays) - len(missing)
    missing.sort(key=lambda p: p["count"], reverse=True)
    yield {
        "stage": "match",
        "message": f"{owned_matches} vorhanden · {len(missing)} fehlen",
        "pct": 32,
    }

    # --- album resolution (network, cached; the longest phase) ----------------
    cache = _load_cache(paths.data_dir)
    resolved = 0
    total_m = len(missing)
    for i, p in enumerate(missing, 1):
        if is_canceled():
            return
        vid = p["videoId"]
        if not resolve or not vid or p.get("hoerspiel") or p.get("podcast"):
            # Hörspiele/podcasts aren't music albums — never resolve them.
            p["album"], p["album_artist"], p["resolved"] = None, p["artist"], False
        else:
            if vid in cache:
                album, art = cache[vid].get("album"), cache[vid].get("artist")
            elif (i - 1) < cap:
                album, art = _lookup_album(vid, p["artist"], p["title"])
                cache[vid] = {"album": album, "artist": art}
            else:
                album, art = None, None  # over the cap — stays unresolved, not dropped
            if album:
                resolved += 1
            p["album"] = album
            p["album_artist"] = art or p["artist"]
            p["resolved"] = bool(album)
        if i % 10 == 0 or i == total_m:
            yield {
                "stage": "resolve",
                "message": f"Alben auflösen … {i}/{total_m}",
                "done": i,
                "total": total_m,
                "pct": 32 + int(60 * i / max(total_m, 1)),
            }
    if resolve:
        _save_cache(paths.data_dir, cache)

    groups, n_songs = _build_groups(missing)
    result = {
        "type": "music",
        "library_size": library.library_size(),
        "history_plays": total_plays,
        "unique_tracks": len(plays),
        "owned_matches": owned_matches,
        "missing_tracks": len(missing),
        "missing_songs": n_songs,
        "resolved_tracks": resolved,
        "unresolved_tracks": len(missing) - resolved,
        "resolve_cap": cap if (resolve and len(missing) > cap) else None,
        "categories": _categories_present(groups),
        "groups": groups,
    }
    yield {"stage": "done", "message": "fertig", "pct": 100, "result": result}


def analyze(history_bytes: bytes, paths: ImporterPaths, **opts) -> dict:
    """Non-streaming convenience wrapper (used by tests)."""
    result: dict = {}
    for ev in analyze_iter(history_bytes, paths, **opts):
        if "result" in ev:
            result = ev["result"]
    return result


# ---------------------------------------------------------------------------
# Grouping: songs -> fewest albums (set cover) -> category/artist/album tree
# ---------------------------------------------------------------------------

CATEGORY_ORDER = ["Musik", "Podcast", "Hörspiel", "Sonstiges"]


def _build_groups(missing: list[dict]) -> tuple[list[dict], int]:
    """Collapse plays to unique songs, then pick the FEWEST albums that cover all
    songs (greedy set cover) so a song appearing on several albums lands on the
    album that minimises how many albums we must acquire. Returns (groups,
    unique_song_count); each group is one album with its per-song play counts."""
    songs: dict[str, dict] = {}
    for p in missing:
        k = track_key(p["artist"], p["title"])
        s = songs.get(k)
        if s is None:
            s = {
                "artist": p.get("album_artist") or p["artist"],
                "title": p["title"],
                "plays": 0,
                "albums": set(),
                "music": False,
                "hoerspiel": False,
                "podcast": False,
            }
            songs[k] = s
        s["plays"] += p["count"]
        if p.get("topic"):
            s["music"] = True
        if p.get("hoerspiel"):
            s["hoerspiel"] = True
        if p.get("podcast"):
            s["podcast"] = True
        if p.get("album"):
            s["albums"].add((p.get("album_artist") or p["artist"], p["album"]))

    album_to_songs: dict[tuple[str, str], set[str]] = {}
    for k, s in songs.items():
        for alb in s["albums"]:
            album_to_songs.setdefault(alb, set()).add(k)

    remaining = {k for k, s in songs.items() if s["albums"]}
    chosen: dict[tuple[str, str], set[str]] = {}
    while remaining:
        best_alb, best_cov, best_score = None, None, None
        for alb, ks in album_to_songs.items():
            cov = ks & remaining
            if not cov:
                continue
            # Prefer covering more songs; on ties prefer a real artist album
            # over a "Various Artists" compilation, then more plays.
            score = (
                len(cov),
                0 if _is_various(alb[0]) else 1,
                sum(songs[k]["plays"] for k in cov),
            )
            if best_score is None or score > best_score:
                best_alb, best_cov, best_score = alb, cov, score
        chosen[best_alb] = best_cov
        remaining -= best_cov

    groups = [_group_entry(a, alb, ks, songs, True) for (a, alb), ks in chosen.items()]
    # Songs without any resolved album — grouped per artist so none are dropped.
    leftover: dict[str, set[str]] = {}
    for k, s in songs.items():
        if not s["albums"]:
            leftover.setdefault(s["artist"], set()).add(k)
    groups += [
        _group_entry(a, UNRESOLVED_LABEL, ks, songs, False)
        for a, ks in leftover.items()
    ]

    groups.sort(key=lambda g: g["plays"], reverse=True)
    return groups, len(songs)


def _group_entry(artist, album, keys, songs, resolved) -> dict:
    slist = sorted(
        ({"title": songs[k]["title"], "plays": songs[k]["plays"]} for k in keys),
        key=lambda x: x["plays"],
        reverse=True,
    )
    # "Kapitel/Folge N" → Hörspiel; a "- Topic" art track or a resolved album →
    # Musik; everything else is "Sonstiges" for the user to re-categorise. We
    # can't reliably tell podcast from Hörspiel from Takeout alone.
    n = len(keys)
    hoer_votes = sum(1 for k in keys if songs[k]["hoerspiel"])
    pod_votes = sum(1 for k in keys if songs[k]["podcast"])
    music_votes = sum(1 for k in keys if songs[k]["music"] or songs[k]["albums"])
    if hoer_votes * 2 >= n:
        category = "Hörspiel"
    elif pod_votes * 2 >= n:
        category = "Podcast"
    elif music_votes:
        category = "Musik"
    else:
        category = "Sonstiges"
    return {
        "category": category,
        "artist": artist,
        "album": album,
        "resolved": resolved,
        "plays": sum(x["plays"] for x in slist),
        "songs": slist,
    }


def _categories_present(groups: list[dict]) -> list[str]:
    present = {g["category"] for g in groups}
    return [c for c in CATEGORY_ORDER if c in present] + sorted(
        present - set(CATEGORY_ORDER)
    )


# ---------------------------------------------------------------------------
# Export (operate on the possibly re-categorised groups the client sends back)
# ---------------------------------------------------------------------------


def to_csv(groups: list[dict]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Kategorie", "Artist", "Album", "Song", "Abspielungen"])
    for g in groups:
        for s in g.get("songs", []):
            w.writerow(
                [
                    g.get("category", "Sonstiges"),
                    g["artist"],
                    g["album"],
                    s["title"],
                    s["plays"],
                ]
            )
    return buf.getvalue()


def to_notes(groups: list[dict], user: str, generated: str) -> str:
    """The shopping list as an OKF-friendly note for the Solaris knowledge layer
    to enrich (physically-owned / wishlist / where-to-acquire). Human-readable
    Markdown + frontmatter Solaris's ingest can key on. See README 'Solaris handoff'."""
    fm = [
        "---",
        "type: music-wishlist",
        "source: solaris-import-google",
        f"resident: {user}",
        f"generated: {generated}",
        "tags: [musik, einkaufsliste, wishlist]",
        "# Solaris: enrich each album with owned_physical / wishlist / source (where to get it).",
        "---",
        "",
    ]
    return "\n".join(fm) + to_markdown(groups)


def to_markdown(groups: list[dict]) -> str:
    by_cat: dict[str, list[dict]] = {}
    for g in groups:
        by_cat.setdefault(g.get("category", "Sonstiges"), []).append(g)
    cats = [c for c in CATEGORY_ORDER if c in by_cat] + [
        c for c in by_cat if c not in CATEGORY_ORDER
    ]
    lines = ["# Musik-Einkaufsliste", ""]
    for cat in cats:
        lines.append(f"## {cat}")
        lines.append("")
        by_artist: dict[str, list[dict]] = {}
        for g in by_cat[cat]:
            by_artist.setdefault(g["artist"], []).append(g)
        for artist in sorted(
            by_artist, key=lambda a: sum(x["plays"] for x in by_artist[a]), reverse=True
        ):
            lines.append(f"### {artist}")
            for g in sorted(by_artist[artist], key=lambda x: x["plays"], reverse=True):
                lines.append(f"- **{g['album']}** — {g['plays']} Abspielungen")
                lines += [f"  - {s['title']} ({s['plays']})" for s in g["songs"]]
            lines.append("")
    return "\n".join(lines)

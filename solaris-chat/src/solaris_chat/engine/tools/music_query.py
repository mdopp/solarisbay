"""Structured music-library query tool (#588).

Music questions ("welche Songs von <Künstler> habe ich") were answered by the
`notes_search` vault grep: a substring match (so "Queen" wrongly matched "Queens
of the Stone Age"), capped at 8 hits, and it leaked raw hash slugs. The
structured projection (`entities`/`facts`, populated by `ingest/jellyfin.py`) is
correct — a band is `entities.type='band'`, a song is `type='song'` with a
`facts(predicate='by', value='bands/<slug>')` edge — but no model tool read it.

This adds ONE token-lean `music_query` tool over that store:

  - `op="songs_by_artist"`: resolve the BAND — EXACT canonical_name (case-
    insensitive) first and it ALWAYS wins (Queen → Queen, never fuzzed to Queens
    of the Stone Age); only when nothing matches exactly does a RANKED FUZZY pass
    over the caller+household bands run (whole-word containment + typo edit-ratio,
    so "Joel" finds "Billy Joel" and "Beatls" finds "The Beatles", returning
    not-found below a threshold rather than a random band) — then follow its `by`
    edge to the songs, return clean `canonical_name` titles (never the hash
    slug), capped + a total.
  - `op="list_artists"`: the type='band' entities, optional name prefix.

Every query is per-owner scoped: `resident_uid IN (caller, 'household')` (caller
from `uid_getter`; an unknown/voice caller is `household`, so it sees only the
shared library — never another resident's private one).
"""

from __future__ import annotations

import json
import re
from difflib import SequenceMatcher
from typing import Any, Protocol

from solaris_chat.engine.knowledge import projection
from solaris_chat.engine.tools import Tool


class LyricsClient(Protocol):
    """The slice of the Jellyfin client `song_lyrics` needs (live fetch)."""

    async def lyrics(self, audio_id: str) -> str | None: ...


_SONG_CAP = 50
_ARTIST_CAP = 50

# Fuzzy band-resolve weights/threshold (only reached when NO exact match exists).
# Three signals blend: (a) WHOLE-WORD containment — a query token is a whole word
# in the name ('joel' in 'Billy Joel', 'queens' in 'Queens of the Stone Age');
# this dominates and keeps 'Queens' on QOTSA, not a typo-near 'Queen'. (b) the
# best per-token edit-ratio against the name's words catches typos ('Beatls' →
# 'Beatles' word of 'The Beatles'). (c) a small full-string ratio + prefix bonus
# break near-ties. The best score must clear the threshold or we return
# not-found rather than a random band.
_FUZZY_WORD_WEIGHT = 0.45
_FUZZY_TOKEN_WEIGHT = 0.45
_FUZZY_FULL_WEIGHT = 0.1
_FUZZY_PREFIX_BONUS = 0.05
_FUZZY_THRESHOLD = 0.45

_WORD_RE = re.compile(r"\w+", re.UNICODE)


def _tokens(text: str) -> list[str]:
    return _WORD_RE.findall(text.lower())


def _fuzzy_score(query: str, candidate: str) -> float:
    q_tokens = _tokens(query)
    c_tokens = _tokens(candidate)
    if not q_tokens or not c_tokens:
        return 0.0
    c_set = set(c_tokens)
    word_frac = sum(1 for t in q_tokens if t in c_set) / len(q_tokens)
    per_token = sum(
        max(SequenceMatcher(None, t, w).ratio() for w in c_tokens) for t in q_tokens
    ) / len(q_tokens)
    full = SequenceMatcher(None, query.lower(), candidate.lower()).ratio()
    prefix = _FUZZY_PREFIX_BONUS if candidate.lower().startswith(query.lower()) else 0.0
    return (
        _FUZZY_WORD_WEIGHT * word_frac
        + _FUZZY_TOKEN_WEIGHT * per_token
        + _FUZZY_FULL_WEIGHT * full
        + prefix
    )


def _escape_like(value: str) -> str:
    """Escape LIKE wildcards so a model-supplied prefix stays a literal prefix.

    `%`/`_` in the arg would otherwise expand into wildcards, coercing the
    anchored-prefix LIKE back into a substring match (Queen → Queens-of-...).
    Used with `ESCAPE '\\'`; `\\` itself is escaped first."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _band_value(okf_path: str) -> str:
    """The `by`-fact value (`bands/<slug>`) a band's okf_path projects to.

    A band lives at `okf/bands/<slug>.md` (shared) or
    `users/<uid>/okf/bands/<slug>.md` (private); the song→band `by` edge stores
    the `okf/`-relative `bands/<slug>` (no owner prefix, no `.md`)."""
    rel = okf_path
    marker = "okf/"
    idx = rel.find(marker)
    if idx != -1:
        rel = rel[idx + len(marker) :]
    if rel.endswith(".md"):
        rel = rel[:-3]
    return rel


def _audio_id(resource: str) -> str:
    """The Jellyfin audio id from a song's `resource` fact
    (`jellyfin://audio/<id>` → `<id>`); empty when not a jellyfin audio URI."""
    prefix = "jellyfin://audio/"
    return resource[len(prefix) :] if resource.startswith(prefix) else ""


def build_music_query_tools(
    db_path: str, uid_getter, jellyfin_client: LyricsClient | None = None
) -> list[Tool]:
    def _caller() -> str:
        return uid_getter() or projection.SHARED_UID

    def _resolve_band_id(conn, artist: str, caller: str) -> str | None:
        # Prefer the shared exact resolver (id / exact canonical_name / alias),
        # but only accept it when it lands on a band — a person/place named the
        # same must not shadow the artist.
        ref = projection.resolve_entity_id(conn, artist, caller)
        if ref is not None:
            row = conn.execute(
                "SELECT id FROM entities WHERE id = ? AND type = 'band'", (ref,)
            ).fetchone()
            if row is not None:
                return row["id"]
        # Scoped exact (case-insensitive) on bands FIRST...
        row = conn.execute(
            "SELECT id FROM entities"
            " WHERE type = 'band' AND resident_uid IN (?, ?)"
            " AND canonical_name = ? COLLATE NOCASE"
            " ORDER BY canonical_name LIMIT 1",
            (caller, projection.SHARED_UID, artist),
        ).fetchone()
        if row is not None:
            return row["id"]
        # ...only then RANKED FUZZY over the caller+household bands (scoped fetch,
        # ranked in Python) so "Joel" finds "Billy Joel" and "Beatls" finds "The
        # Beatles" — but an exact match above already short-circuited, so "Queen"
        # never fuzzes to "Queens of the Stone Age". A fuzzy match must never
        # surface another resident's private band, hence the same resident scope.
        candidates = conn.execute(
            "SELECT id, canonical_name FROM entities"
            " WHERE type = 'band' AND resident_uid IN (?, ?)"
            " ORDER BY canonical_name",
            (caller, projection.SHARED_UID),
        ).fetchall()
        best_id: str | None = None
        best_score = 0.0
        for cand in candidates:
            score = _fuzzy_score(artist, cand["canonical_name"])
            if score > best_score:
                best_score, best_id = score, cand["id"]
        return best_id if best_score >= _FUZZY_THRESHOLD else None

    def _song_by_value(conn, song_id: str, caller: str) -> str | None:
        # The song's `by` edge value (`bands/<slug>`), scoped to the caller.
        row = conn.execute(
            "SELECT value FROM facts"
            " WHERE subject_entity_id = ? AND predicate = 'by'"
            " AND resident_uid IN (?, ?) LIMIT 1",
            (song_id, caller, projection.SHARED_UID),
        ).fetchone()
        return row["value"] if row is not None else None

    def _resolve_song_id(conn, title: str, artist: str, caller: str) -> str | None:
        # Exact canonical_name (case-insensitive) over the caller+household songs
        # first; only then a ranked fuzzy pass over the same scoped rows. When an
        # artist is given, prefer (among the exact, else among the top fuzzy ties)
        # the song whose `by` band matches that artist — so two songs sharing a
        # title disambiguate to the right artist's.
        want_value: str | None = None
        if artist:
            band_id = _resolve_band_id(conn, artist, caller)
            if band_id is not None:
                okf_path = projection.entity_okf_path(conn, band_id)
                if okf_path is not None:
                    want_value = _band_value(okf_path)
        exact = conn.execute(
            "SELECT id FROM entities"
            " WHERE type = 'song' AND resident_uid IN (?, ?)"
            " AND canonical_name = ? COLLATE NOCASE"
            " ORDER BY canonical_name",
            (caller, projection.SHARED_UID, title),
        ).fetchall()
        if exact:
            ids = [r["id"] for r in exact]
            if want_value is not None:
                for sid in ids:
                    if _song_by_value(conn, sid, caller) == want_value:
                        return sid
            return ids[0]
        candidates = conn.execute(
            "SELECT id, canonical_name FROM entities"
            " WHERE type = 'song' AND resident_uid IN (?, ?)"
            " ORDER BY canonical_name",
            (caller, projection.SHARED_UID),
        ).fetchall()
        best_id: str | None = None
        best_score = 0.0
        for cand in candidates:
            score = _fuzzy_score(title, cand["canonical_name"])
            # An artist match breaks fuzzy ties toward the right artist's song.
            if (
                want_value is not None
                and _song_by_value(conn, cand["id"], caller) == want_value
            ):
                score += _FUZZY_PREFIX_BONUS
            if score > best_score:
                best_score, best_id = score, cand["id"]
        return best_id if best_score >= _FUZZY_THRESHOLD else None

    async def song_lyrics(title: str, artist: str) -> str:
        title = title.strip()
        artist = artist.strip()
        if not title:
            return json.dumps({"error": "title required"}, ensure_ascii=False)
        caller = _caller()
        conn = projection.open_conn(db_path)
        try:
            song_id = _resolve_song_id(conn, title, artist, caller)
            if song_id is None:
                return json.dumps({"found": False}, ensure_ascii=False)
            song = projection.entity_row(conn, song_id)
            clean_title = song["canonical_name"]
            band_value = _song_by_value(conn, song_id, caller)
            artist_name = ""
            if band_value:
                rows = projection.fetch_all(
                    conn,
                    "SELECT e.canonical_name FROM concepts c"
                    " JOIN entities e ON e.id = c.ref_id"
                    " WHERE c.ref_kind = 'entity' AND e.type = 'band'"
                    " AND e.resident_uid IN (?, ?)"
                    " AND (c.okf_path LIKE '%okf/' || ? || '.md')",
                    (caller, projection.SHARED_UID, band_value),
                )
                if rows:
                    artist_name = rows[0]["canonical_name"]
            resource = ""
            for f in projection.entity_facts(conn, song_id, caller):
                if f["predicate"] == "resource":
                    resource = f["value"]
                    break
            audio_id = _audio_id(resource)
        finally:
            conn.close()
        lyrics = None
        if jellyfin_client is not None and audio_id:
            lyrics = await jellyfin_client.lyrics(audio_id)
        if lyrics:
            out: dict[str, Any] = {"title": clean_title, "lyrics": lyrics}
            if artist_name:
                out["artist"] = artist_name
            return json.dumps(out, ensure_ascii=False)
        result: dict[str, Any] = {
            "title": clean_title,
            "lyrics": None,
            "note": "keine Lyrics verfügbar",
        }
        if artist_name:
            result["artist"] = artist_name
        return json.dumps(result, ensure_ascii=False)

    async def songs_by_artist(artist: str, limit: int) -> str:
        artist = artist.strip()
        if not artist:
            return json.dumps({"error": "artist required"}, ensure_ascii=False)
        caller = _caller()
        conn = projection.open_conn(db_path)
        try:
            band_id = _resolve_band_id(conn, artist, caller)
            if band_id is None:
                return json.dumps(
                    {"artist": artist, "total": 0, "songs": []}, ensure_ascii=False
                )
            band = projection.entity_row(conn, band_id)
            okf_path = projection.entity_okf_path(conn, band_id)
            if okf_path is None:
                return json.dumps(
                    {"artist": band["canonical_name"], "total": 0, "songs": []},
                    ensure_ascii=False,
                )
            value = _band_value(okf_path)
            rows = projection.fetch_all(
                conn,
                "SELECT e.canonical_name FROM facts f"
                " JOIN entities e ON e.id = f.subject_entity_id"
                " WHERE f.predicate = 'by' AND f.value = ?"
                " AND e.type = 'song' AND e.resident_uid IN (?, ?)"
                " ORDER BY e.canonical_name",
                (value, caller, projection.SHARED_UID),
            )
            titles = [r["canonical_name"] for r in rows]
            return json.dumps(
                {
                    "artist": band["canonical_name"],
                    "total": len(titles),
                    "songs": titles[:limit],
                },
                ensure_ascii=False,
            )
        finally:
            conn.close()

    async def artist_info(artist: str) -> str:
        artist = artist.strip()
        if not artist:
            return json.dumps({"error": "artist required"}, ensure_ascii=False)
        caller = _caller()
        conn = projection.open_conn(db_path)
        try:
            band_id = _resolve_band_id(conn, artist, caller)
            if band_id is None:
                return json.dumps(
                    {"artist": artist, "found": False}, ensure_ascii=False
                )
            band = projection.entity_row(conn, band_id)
            # Caller-scoped facts (resident_uid IN (caller, 'household')) — a
            # private band's genre/bio never surfaces to another resident.
            facts = projection.entity_facts(conn, band_id, caller)
            by_predicate = {f["predicate"]: f["value"] for f in facts}
            okf_path = projection.entity_okf_path(conn, band_id)
            song_count = 0
            if okf_path is not None:
                value = _band_value(okf_path)
                rows = projection.fetch_all(
                    conn,
                    "SELECT COUNT(*) AS n FROM facts f"
                    " JOIN entities e ON e.id = f.subject_entity_id"
                    " WHERE f.predicate = 'by' AND f.value = ?"
                    " AND e.type = 'song' AND e.resident_uid IN (?, ?)",
                    (value, caller, projection.SHARED_UID),
                )
                song_count = rows[0]["n"]
            return json.dumps(
                {
                    "artist": band["canonical_name"],
                    "genre": by_predicate.get("genre", ""),
                    "bio": by_predicate.get("bio", ""),
                    "song_count": song_count,
                },
                ensure_ascii=False,
            )
        finally:
            conn.close()

    async def list_artists(prefix: str, limit: int) -> str:
        caller = _caller()
        conn = projection.open_conn(db_path)
        try:
            sql = (
                "SELECT canonical_name FROM entities"
                " WHERE type = 'band' AND resident_uid IN (?, ?)"
            )
            params: list[Any] = [caller, projection.SHARED_UID]
            if prefix:
                sql += " AND canonical_name LIKE ? || '%' ESCAPE '\\' COLLATE NOCASE"
                params.append(_escape_like(prefix))
            sql += " ORDER BY canonical_name"
            rows = projection.fetch_all(conn, sql, tuple(params))
            names = [r["canonical_name"] for r in rows]
            return json.dumps(
                {"total": len(names), "artists": names[:limit]}, ensure_ascii=False
            )
        finally:
            conn.close()

    async def music_query(args: dict[str, Any]) -> str:
        op = str(args.get("op") or "").strip()
        limit = args.get("limit")
        if op == "songs_by_artist":
            cap = int(limit) if isinstance(limit, int) and limit > 0 else _SONG_CAP
            return await songs_by_artist(
                str(args.get("artist") or ""), min(cap, _SONG_CAP)
            )
        if op == "list_artists":
            cap = int(limit) if isinstance(limit, int) and limit > 0 else _ARTIST_CAP
            return await list_artists(
                str(args.get("prefix") or "").strip(), min(cap, _ARTIST_CAP)
            )
        if op == "artist_info":
            return await artist_info(str(args.get("artist") or ""))
        if op == "song_lyrics":
            return await song_lyrics(
                str(args.get("title") or ""), str(args.get("artist") or "")
            )
        return json.dumps(
            {
                "error": "op must be songs_by_artist, list_artists,"
                " artist_info or song_lyrics"
            },
            ensure_ascii=False,
        )

    return [
        Tool(
            name="music_query",
            description=(
                "Beantwortet Fragen zur eigenen Musikbibliothek aus dem"
                " strukturierten Wissensspeicher (nicht notes_search)."
                " op='songs_by_artist' mit artist=<Name>: welche Songs/Lieder"
                " von <Künstler> habe ich. op='list_artists' (optional prefix):"
                " welche Künstler/Bands habe ich in der Bibliothek."
                " op='artist_info' mit artist=<Name>: was weiß ich über"
                " <Künstler/Band>, erzähl mir was über <Band>, welches Genre"
                " ist <Band> — liefert Genre, Kurzbio und Songanzahl."
                " op='song_lyrics' mit title=<Songtitel> (optional artist):"
                " zeig mir die Lyrics von <Song>, Songtext von <Song> — holt"
                " den Liedtext live aus der Bibliothek. Liefert saubere Titel."
                " Exakter Treffer gewinnt (Queen ≠ Queens of the Stone Age);"
                " sonst unscharf (Joel → Billy Joel)."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "op": {
                        "type": "string",
                        "enum": [
                            "songs_by_artist",
                            "list_artists",
                            "artist_info",
                            "song_lyrics",
                        ],
                    },
                    "artist": {"type": "string"},
                    "title": {"type": "string"},
                    "prefix": {"type": "string"},
                    "limit": {"type": "integer"},
                },
                "required": ["op"],
            },
            handler=music_query,
        ),
    ]

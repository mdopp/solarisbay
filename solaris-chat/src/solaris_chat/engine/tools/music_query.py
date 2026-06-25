"""Structured music-library query tool (#588).

Music questions ("welche Songs von <Künstler> habe ich") were answered by the
`notes_search` vault grep: a substring match (so "Queen" wrongly matched "Queens
of the Stone Age"), capped at 8 hits, and it leaked raw hash slugs. The
structured projection (`entities`/`facts`, populated by `ingest/jellyfin.py`) is
correct — a band is `entities.type='band'`, a song is `type='song'` with a
`facts(predicate='by', value='bands/<slug>')` edge — but no model tool read it.

This adds ONE token-lean `music_query` tool over that store:

  - `op="songs_by_artist"`: resolve the BAND exactly (EXACT canonical_name first,
    only then a `LIKE 'name%'` prefix — NEVER a bare `%name%` substring, so Queen
    never matches Queens of the Stone Age), follow its `by` edge to the songs,
    return clean `canonical_name` titles (never the hash slug), capped + a total.
  - `op="list_artists"`: the type='band' entities, optional name prefix.

Every query is per-owner scoped: `resident_uid IN (caller, 'household')` (caller
from `uid_getter`; an unknown/voice caller is `household`, so it sees only the
shared library — never another resident's private one).
"""

from __future__ import annotations

import json
from typing import Any

from solaris_chat.engine.knowledge import projection
from solaris_chat.engine.tools import Tool

_SONG_CAP = 50
_ARTIST_CAP = 50


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


def build_music_query_tools(db_path: str, uid_getter) -> list[Tool]:
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
        # ...only then a PREFIX (`name%`), never a bare `%name%` substring, so
        # "Queen" can never match "Queens of the Stone Age".
        row = conn.execute(
            "SELECT id FROM entities"
            " WHERE type = 'band' AND resident_uid IN (?, ?)"
            " AND canonical_name LIKE ? || '%' ESCAPE '\\' COLLATE NOCASE"
            " ORDER BY canonical_name LIMIT 1",
            (caller, projection.SHARED_UID, _escape_like(artist)),
        ).fetchone()
        return row["id"] if row is not None else None

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
        return json.dumps(
            {"error": "op must be songs_by_artist or list_artists"},
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
                " welche Künstler/Bands habe ich in der Bibliothek. Liefert"
                " saubere Titel, kein Substring-Treffer (Queen ≠ Queens of the"
                " Stone Age)."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "op": {
                        "type": "string",
                        "enum": ["songs_by_artist", "list_artists"],
                    },
                    "artist": {"type": "string"},
                    "prefix": {"type": "string"},
                    "limit": {"type": "integer"},
                },
                "required": ["op"],
            },
            handler=music_query,
        ),
    ]

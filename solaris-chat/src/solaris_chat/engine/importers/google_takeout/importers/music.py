"""YouTube-Music history → `wishlist` album facts (#868, P3, ADR 0002/0003/0004).

The standalone `solaris-import-google` tool turned a Takeout `watch-history.json`
into a music SHOPPING-LIST NOTE (markdown, delivered over Syncthing). In-engine
the substrate is different: a played album a resident does not yet own is a
QUERYABLE signal on the **album entity**, not a materialized list. So this kind
runs the vendored ``music_shopping.analyze_iter`` (play aggregation, ytmusicapi
album resolution, greedy set-cover, fuzzy ownership — the "prefer unresolved over
wrong" guard is kept) under the durable job runner, and for each resolved album
writes a source-tagged ``wishlist`` fact (source=import) + a ``play_count`` fact
onto the album entity (resolved/created by P1a's ``Artist – Album`` canonical_name
+ ``{artist_slug}-{album_slug}`` slug, so it dedups with Jellyfin's/notes'/chat's
album node — one entity, many sources).

The acquire list is NOT written here: it is the existing ``music_query
op="wishlist"`` (#879), which reads these ``wishlist`` facts and suppresses an
album the resident already owns physically or has digitally. No per-song markdown,
no wishlist note.

Idempotent + per-resident (job owner): a re-run replaces the source=import facts
on the same album node — no duplicate album entities.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from solaris_chat.engine.knowledge import ConceptRecord, safe_slug
from solaris_chat.engine.knowledge.writer import OkfWriter
from solaris_chat.engine.ollama import OllamaChat, OllamaError

from .. import ImportPlan, catalog
from ..music_shopping import analyze_iter
from ..paths import ImporterPaths

# Source tag for an import-derived album fact (ADR 0003 — one entity, many
# sources: it coexists with Jellyfin's `by`/`on_album` and a note's
# `owned_physical`). The wishlist query (#879) reads the `wishlist` predicate.
_SOURCE = "import"

# The LLM classification prompt. The model returns one bare label; anything else
# (or an error) makes `catalog.classify` fall back to the shipped seed lists.
_CLASSIFY_SYS = (
    "Klassifiziere den folgenden YouTube-Titel in genau EIN Wort:"
    " 'Podcast' (eine Podcast-Folge), 'Hörspiel' (Hörspiel/Hörbuch-Kapitel),"
    " oder 'Musik' (ein Musikstück/Song/Album). Antworte NUR mit dem einen Wort."
)


def _llm_classifier(ollama_url: str, model: str):
    """A ``fn(artist, title) -> "Podcast"|"Hörspiel"|"Musik"|None`` backed by the
    engine LLM. Fail-open: any error / unreachable Ollama returns None, so
    ``catalog.classify`` degrades to its mechanical seed lists."""
    client = OllamaChat(ollama_url)

    async def _ask(artist: str, title: str) -> str | None:
        msgs = [
            {"role": "system", "content": _CLASSIFY_SYS},
            {"role": "user", "content": f"Kanal: {artist}\nTitel: {title}"},
        ]
        result = None
        async for kind, payload in client.stream(
            model,
            msgs,
            tools=None,
            think=False,
            options={"num_predict": 8, "temperature": 0.0},
        ):
            if kind == "done":
                result = payload
        return result.content.strip() if result is not None else None

    def classify(artist: str, title: str) -> str | None:
        try:
            raw = asyncio.run(_ask(artist, title))
        except (OllamaError, OSError, RuntimeError, ValueError):
            return None
        if not raw:
            return None
        low = raw.lower()
        if "podcast" in low:
            return "Podcast"
        if "hörspiel" in low or "hoerspiel" in low or "hörbuch" in low:
            return "Hörspiel"
        if "musik" in low:
            return "Musik"
        return None

    return classify


def _write_album_facts(
    writer: OkfWriter, owner_uid: str, artist: str, album: str, plays: int
) -> bool:
    """Resolve/create the album entity by P1a's ``Artist – Album`` canonical_name
    + ``{artist_slug}-{album_slug}`` slug and write source=import ``wishlist`` +
    ``play_count`` facts. Returns True when written (not an unchanged re-run)."""
    try:
        artist_slug = safe_slug(artist)
        album_slug = safe_slug(album)
    except ValueError:
        return False
    rec = ConceptRecord(
        type="album",
        title=f"{artist} – {album}",
        slug=f"{artist_slug}-{album_slug}",
        source=_SOURCE,
        external_id=f"import:{owner_uid}:{artist_slug}-{album_slug}",
        resident=owner_uid,
        facts=[("wishlist", ""), ("play_count", str(plays))],
        # Import owns no markdown for the album — the entity + facts are the
        # signal; a Jellyfin/note ingest keeps the RAG-worthy album markdown.
        projection_only=True,
    )
    return not writer.write_concept(rec, ingesting_uid=owner_uid).skipped


def run_music_import(
    history_bytes: bytes,
    paths: ImporterPaths,
    *,
    owner_uid: str,
    db_path: str,
    notes_dir: str,
    ollama_url: str,
    model: str,
    is_canceled=None,
    **opts: Any,
):
    """Generator: run ``analyze_iter`` then write ``wishlist``/``play_count``
    album facts for each resolved album the owner played. Yields the analysis
    progress events, then a final event carrying the write summary as ``result``.

    The LLM classifier is installed for the run's duration (mechanical fallback
    inside ``catalog.classify``) and cleared afterwards so it never leaks into an
    unrelated call."""
    catalog.set_llm_classifier(_llm_classifier(ollama_url, model))
    try:
        result: dict[str, Any] = {}
        for ev in analyze_iter(history_bytes, paths, is_canceled=is_canceled, **opts):
            if "result" in ev:
                result = ev["result"]
            else:
                yield ev
        if is_canceled is not None and is_canceled():
            return

        writer = OkfWriter(db_path=db_path, notes_dir=notes_dir)
        written = 0
        for g in result.get("groups", []):
            if not g.get("resolved") or g.get("category") != "Musik":
                continue
            if _write_album_facts(
                writer, owner_uid, g["artist"], g["album"], int(g.get("plays", 0))
            ):
                written += 1
        yield {
            "stage": "done",
            "message": f"{written} Alben zur Wunschliste",
            "pct": 100,
            "result": {
                "type": "music",
                "albums_written": written,
                "resolved_tracks": result.get("resolved_tracks", 0),
                "missing_songs": result.get("missing_songs", 0),
            },
        }
    finally:
        catalog.set_llm_classifier(None)


class MusicImporter:
    """The ``music`` (ytmusic) importer kind (#868). ``run`` drives the album-fact
    write path; ``detect``/``plan`` are the archive-manifest surface the shared
    ``Importer`` protocol requires (a Takeout ``watch-history.json`` claim)."""

    kind = "music"

    def detect(self, manifest: Any) -> list[dict[str, Any]]:
        names = manifest if isinstance(manifest, list) else []
        if any(str(n).endswith("watch-history.json") for n in names):
            return [{"kind": self.kind, "datatype": "ytmusic"}]
        return []

    def plan(self, archive: Any, selections: Any) -> ImportPlan:
        return ImportPlan(kind=self.kind)

    def run(self, plan: ImportPlan, progress: Any) -> list[dict[str, Any]]:
        raise NotImplementedError("music import runs via the durable job runner")


def _music_runner_factory(payload: dict[str, Any]):
    """Build the durable-job factory for the ``music`` kind from its payload.

    Payload carries ``history`` (the watch-history.json bytes as a UTF-8 or
    latin-1-safe string), the owner + on-disk paths, the LLM endpoint, and the
    bounded ``analyze_iter`` options (min_plays/months/resolve/cap)."""
    history_bytes = payload["history"].encode("utf-8")
    paths = ImporterPaths(
        radicale_data=Path(payload.get("radicale_data", "")),
        music_dir=Path(payload["music_dir"]),
        data_dir=Path(payload["data_dir"]),
    )
    opts = {
        k: payload[k] for k in ("min_plays", "months", "resolve", "cap") if k in payload
    }

    def factory(is_canceled):
        return run_music_import(
            history_bytes,
            paths,
            owner_uid=payload["owner_uid"],
            db_path=payload["db_path"],
            notes_dir=payload["notes_dir"],
            ollama_url=payload["ollama_url"],
            model=payload["model"],
            is_canceled=is_canceled,
            **opts,
        )

    return factory

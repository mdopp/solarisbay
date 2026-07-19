"""One-shot prune of legacy per-song OKF artifacts (#878, ADR 0002/B7).

Before #877 made externally-sourced Jellyfin `song`s projection-only, each song
also materialized an OKF markdown file, a `concepts` link row, and an
`okf_vectors` embedding. Those stale artifacts must go so a pruned song is
identical to a freshly projected one — entity + `on_album`/`by` facts only.

Idempotent: a projection-only song has no `concepts` row, so the join finds
only pre-switch songs; after one pass a re-run matches nothing. Per-resident
safe: it keys off `(source="jellyfin", type="song")` and deletes exactly the
matched concept's own file/FTS/rows — never another resident's data, never
album/artist (they keep their lean markdown + embedding).

Rides `run_ingest` (the single boot + nightly ingest call site), next to the
embedding drain — no new thread/loop/knob.
"""

from __future__ import annotations

from pathlib import Path

from solaris_chat import notes_index
from solaris_chat.logging import log

from ..knowledge import projection

_SOURCE = "jellyfin"
_TYPE = "song"


def prune_legacy_song_artifacts(db_path: str, notes_dir: str) -> int:
    """Delete every legacy per-song OKF artifact; return the count pruned.

    Keeps the song entity + its facts; removes the markdown file, its FTS row,
    the `concepts` link row, and the `okf_vectors` embedding. Never raises."""
    try:
        root = Path(notes_dir)
        conn = projection.open_conn(db_path)
        try:
            stale = projection.legacy_projection_only_concepts(
                conn, source=_SOURCE, type=_TYPE
            )
            for row in stale:
                path = (root / row["okf_path"]).resolve()
                path.unlink(missing_ok=True)
                notes_index.ensure_schema(conn)
                notes_index._delete_row(conn, row["okf_path"])
                projection.delete_concept_artifacts(
                    conn,
                    concept_id=row["concept_id"],
                    embedding_id=row["embedding_id"],
                )
            conn.commit()
        finally:
            conn.close()
        if stale:
            log.info("engine.prune.legacy_songs", pruned=len(stale))
        return len(stale)
    except Exception as e:  # noqa: BLE001 — the prune must never crash the ingest.
        log.error("engine.prune.legacy_songs_failed", error=str(e))
        return 0

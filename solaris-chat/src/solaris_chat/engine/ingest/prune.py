"""One-shot prune of legacy per-item OKF artifacts (#878, ADR 0002/B7).

Before #877 made externally-sourced Jellyfin items projection-only, each song —
and now each album and band too — materialized an OKF markdown file, a `concepts`
link row, and an `okf_vectors` embedding. Those stale artifacts must go so a
pruned item is identical to a freshly projected one — entity + facts (`by`,
`on_album`, `genre`, `bio`) only. The same holds for Immich photos once they
became projection-only events (a photo is externally re-ingestable, so no
per-photo markdown belongs in the vault); the photo prune mirrors the music one
against the events-table projection.

Idempotent: a projection-only item has no `concepts` row, so the join finds only
pre-switch items; after one pass a re-run matches nothing. Per-resident safe: it
keys off `(source="jellyfin", type=…)` / `(source="immich", kind="photo")` and
deletes exactly the matched concept's own file/FTS/rows — never another
resident's data, never a non-music entity.

Rides `run_ingest` (the single boot + nightly ingest call site), next to the
embedding drain — no new thread/loop/knob.
"""

from __future__ import annotations

from pathlib import Path

from solaris_chat import notes_index
from solaris_chat.logging import log

from ..knowledge import okf, projection

_SOURCE = "jellyfin"
# Every Jellyfin type is projection-only now (entity + facts, no markdown); each
# owns its whole OKF domain dir, so the dir sweep below is safe per domain.
_MUSIC_DOMAINS = (("song", "songs"), ("album", "albums"), ("band", "bands"))
_PHOTO_SOURCE = "immich"
_PHOTO_KIND = "photo"
# Where empty note/journal/preference shells accumulate (never the externally
# sourced music/photo domains — those are handled above and carry no prose).
_NOTE_SHELL_DIRS = ("okf/notes", "journal", "preferences")


def prune_legacy_music_artifacts(db_path: str, notes_dir: str) -> int:
    """Delete every legacy per-song/album/band OKF artifact; return count pruned.

    Keeps each entity + its facts; removes the markdown file, its FTS row, the
    `concepts` link row, and the `okf_vectors` embedding. Never raises."""
    try:
        root = Path(notes_dir)
        conn = projection.open_conn(db_path)
        notes_index.ensure_schema(conn)
        pruned = swept = 0
        try:
            for etype, domain in _MUSIC_DOMAINS:
                # 1. Concept-linked legacy items: drop the file + FTS row +
                #    concepts link row + okf_vectors embedding, keep entity+facts.
                for row in projection.legacy_projection_only_concepts(
                    conn, source=_SOURCE, type=etype
                ):
                    (root / row["okf_path"]).resolve().unlink(missing_ok=True)
                    notes_index._delete_row(conn, row["okf_path"])
                    projection.delete_concept_artifacts(
                        conn,
                        concept_id=row["concept_id"],
                        embedding_id=row["embedding_id"],
                    )
                    pruned += 1
                # 2. Orphaned stubs (#878): the type is projection-only now, so NO
                #    markdown belongs under its domain dir. The concept-keyed pass
                #    misses files whose concept row was already dropped or whose
                #    stored okf_path never matched the file on disk (re-slugged over
                #    time) — on the real library that left ~12k song stubs behind.
                #    Sweep the domain dir(s) directly; idempotent (dir ends empty).
                domain_dirs = [
                    root / "okf" / domain,
                    *root.glob(f"users/*/okf/{domain}"),
                ]
                for ddir in domain_dirs:
                    if not ddir.is_dir():
                        continue
                    for md in ddir.rglob("*.md"):
                        rel = str(md.relative_to(root))
                        md.unlink(missing_ok=True)
                        notes_index._delete_row(conn, rel)
                        swept += 1
            conn.commit()
        finally:
            conn.close()
        if pruned or swept:
            log.info("engine.prune.legacy_music", pruned=pruned, swept=swept)
        return pruned + swept
    except Exception as e:  # noqa: BLE001 — the prune must never crash the ingest.
        log.error("engine.prune.legacy_music_failed", error=str(e))
        return 0


def prune_legacy_photo_artifacts(db_path: str, notes_dir: str) -> int:
    """Delete every legacy per-photo OKF artifact; return the count pruned.

    Immich photos are events; making them projection-only (like Jellyfin songs)
    means no per-photo markdown belongs in the vault. Keeps the events-table row
    + `event_entities` (face/place edges); removes the markdown file, its FTS
    row, the `concepts` link row, and the `okf_vectors` embedding.

    Concept-keyed only (no dir sweep): `okf/events/**` is a mixed domain — a
    calendar/journal/trip event keeps its full markdown — so a blind sweep would
    be wrong. The join keys on `(kind="photo", source="immich")`, matching only
    photo events that still carry a pre-switch `concepts` row. Idempotent (a
    pruned photo has no `concepts` row, so a re-run matches nothing). Never
    raises."""
    try:
        root = Path(notes_dir)
        conn = projection.open_conn(db_path)
        notes_index.ensure_schema(conn)
        pruned = 0
        try:
            stale = projection.legacy_projection_only_events(
                conn, source=_PHOTO_SOURCE, kind=_PHOTO_KIND
            )
            for row in stale:
                path = (root / row["okf_path"]).resolve()
                path.unlink(missing_ok=True)
                notes_index._delete_row(conn, row["okf_path"])
                projection.delete_concept_artifacts(
                    conn,
                    concept_id=row["concept_id"],
                    embedding_id=row["embedding_id"],
                )
                # Re-key the legacy random-id event row to the deterministic id
                # the projection-only write path now derives from the OKF path,
                # so a later re-ingest (if Immich is reconfigured) updates this
                # row instead of minting a duplicate.
                det = okf.deterministic_id(row["okf_path"])
                if det != row["event_id"]:
                    projection.rekey_event(conn, old_id=row["event_id"], new_id=det)
                pruned += 1
            conn.commit()
        finally:
            conn.close()
        if pruned:
            log.info("engine.prune.legacy_photos", pruned=pruned)
        return pruned
    except Exception as e:  # noqa: BLE001 — the prune must never crash the ingest.
        log.error("engine.prune.legacy_photos_failed", error=str(e))
        return 0


def prune_empty_note_shells(db_path: str, notes_dir: str) -> int:
    """Delete empty note/journal/preference shells; return the count pruned.

    A shell is a markdown file that is only frontmatter, headings, and `—`
    placeholders (an untouched daily-chronicle template, a title-only agent
    "Internal log: …", a bare preference stub) — noise, not knowledge. Removes
    the file, its FTS row, and any projection (concepts / embedding / note
    entity + facts). The note writer now rejects these at the source; this
    cleans up the ones already on disk.

    Two passes: (1) on-disk shells under the note dirs; (2) orphan note concepts
    whose file is already gone (a partial prior prune). Commits per item so one
    bad row can't roll back the whole batch. Idempotent. Never raises."""
    try:
        root = Path(notes_dir)
        conn = projection.open_conn(db_path)
        notes_index.ensure_schema(conn)
        pruned = 0
        try:
            bases: list[Path] = []
            for d in _NOTE_SHELL_DIRS:
                bases.append(root / d)
                bases.extend(root.glob(f"users/*/{d}"))
            for base in bases:
                if not base.is_dir():
                    continue
                for md in base.rglob("*.md"):
                    text = md.read_text(encoding="utf-8", errors="ignore")
                    if not okf.is_empty_note_shell(text):
                        continue
                    rel = str(md.relative_to(root))
                    md.unlink(missing_ok=True)
                    notes_index._delete_row(conn, rel)
                    projection.delete_note_by_okf_path(conn, rel)
                    conn.commit()
                    pruned += 1
            # Pass 2: note concepts whose file no longer exists (an earlier
            # partial prune unlinked the file but rolled its rows back).
            for rel in projection.note_concept_paths(conn):
                if (root / rel).exists():
                    continue
                notes_index._delete_row(conn, rel)
                projection.delete_note_by_okf_path(conn, rel)
                conn.commit()
                pruned += 1
        finally:
            conn.close()
        if pruned:
            log.info("engine.prune.empty_notes", pruned=pruned)
        return pruned
    except Exception as e:  # noqa: BLE001 — the prune must never crash the ingest.
        log.error("engine.prune.empty_notes_failed", error=str(e))
        return 0

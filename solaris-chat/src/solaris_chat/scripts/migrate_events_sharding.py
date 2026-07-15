"""One-time runtime migration: shard flat `okf/events/*.md` by year (#830b).

The immich backfill left ~76k event notes in a single flat `okf/events/` dir.
#830b shards new writes into `okf/events/<year>/<slug>.md`; this script moves the
existing flat notes into the same layout, on the BOX against the live vault
(`/opt/data/notes`) — NOT an alembic migration.

For each flat event note it:
  1. derives the year (frontmatter `timestamp:` first, else the `YYYY-MM-DD`
     date prefix baked into the filename by okf_path);
  2. `os.rename`s the file to `.../okf/events/<year>/<slug>.md`;
  3. updates the `concepts.okf_path` row (so `_existing_event_id` and the
     concept page keep pointing at the moved file); and
  4. **re-points the 830a FTS index** — those tables are keyed by note *path*,
     so a move that skips this leaves `notes_search` blind to exactly the 76k
     notes we just moved. We drop the stale `fts_notes`/`fts_notes_meta` row at
     the old path and re-index the note at the new path in the same step.

Idempotent + resumable: an already-sharded note (a `<year>/` segment after
`events/`) is skipped, so a second run — or a resume after interruption — is a
no-op. Chunked commits + progress logs keep the 76k-file pass observable and
crash-safe. Nothing is deleted except via the rename. `--dry-run` reports the
plan without touching the filesystem or the db.
"""

from __future__ import annotations

import argparse
import os
import re
import sqlite3
from pathlib import Path

from solaris_chat import notes_index
from solaris_chat.config import Settings
from solaris_chat.logging import log

# A flat event note: `.../okf/events/<slug>.md` where <slug> has no further `/`
# (an already-sharded note has `.../okf/events/<year>/<slug>.md`, so its segment
# after `events/` is a directory, not the leaf).
_FLAT_EVENT_RE = re.compile(r"(?P<prefix>(?:.*/)?okf/events)/(?P<leaf>[^/]+\.md)$")

# The `YYYY-MM-DD` date prefix okf_path bakes into an event slug — the year
# fallback when a note carries no frontmatter `timestamp:`.
_SLUG_DATE_RE = re.compile(r"^(?P<year>\d{4})-\d{2}-\d{2}-")

# The frontmatter `timestamp:` line (the authoritative event date, §3).
_TS_RE = re.compile(r"^timestamp:\s*(?P<ts>\d{4})-\d{2}-\d{2}")


def _year_of(path: Path, leaf: str) -> str | None:
    """The event's year: frontmatter `timestamp:` first, else the slug date."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    in_fm = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped == "---":
            if in_fm:
                break  # end of frontmatter — no timestamp line
            in_fm = True
            continue
        m = _TS_RE.match(stripped)
        if m:
            return m.group("ts")
    m = _SLUG_DATE_RE.match(leaf)
    return m.group("year") if m else None


def _plan_move(rel: str, root: Path) -> tuple[str, str] | None:
    """(`new_rel`, `year`) for a flat event note, or None to skip it.

    None means: not a flat event note, already sharded, or no derivable year."""
    m = _FLAT_EVENT_RE.match(rel)
    if not m:
        return None
    leaf = m.group("leaf")
    year = _year_of(root / rel, leaf)
    if not year:
        return None
    return f"{m.group('prefix')}/{year}/{leaf}", year


def migrate(
    db_path: str, notes_dir: str, *, dry_run: bool = False, chunk: int = 500
) -> dict[str, int]:
    """Shard every flat `okf/events/*.md` note under `notes_dir` by year.

    Returns counts {scanned, moved, skipped, no_year}. Commits every `chunk`
    moves so a 76k-file run is crash-safe and resumable; `dry_run` reports the
    plan without mutating anything."""
    root = Path(notes_dir)
    stats = {"scanned": 0, "moved": 0, "skipped": 0, "no_year": 0}
    if not root.is_dir():
        return stats

    conn = sqlite3.connect(db_path, timeout=30)
    conn.execute("PRAGMA busy_timeout = 10000")
    conn.row_factory = sqlite3.Row
    try:
        notes_index.ensure_schema(conn)
        pending = 0
        # Walk the events dirs only. Sorted, so a resume re-covers the same order
        # and already-moved notes (now under <year>/) are simply re-skipped.
        for events_dir in sorted(root.glob("**/okf/events")):
            for entry in sorted(events_dir.iterdir()):
                if entry.is_dir() or entry.suffix != ".md":
                    continue  # a `<year>/` subdir or a stray non-note
                stats["scanned"] += 1
                rel = str(entry.relative_to(root))
                plan = _plan_move(rel, root)
                if plan is None:
                    stats["no_year"] += 1
                    continue
                new_rel, _year = plan
                if new_rel == rel:
                    stats["skipped"] += 1
                    continue
                if dry_run:
                    stats["moved"] += 1
                    continue
                new_path = root / new_rel
                new_path.parent.mkdir(parents=True, exist_ok=True)
                os.rename(entry, new_path)
                conn.execute(
                    "UPDATE concepts SET okf_path = ? "
                    "WHERE ref_kind = 'event' AND okf_path = ?",
                    (new_rel, rel),
                )
                # Re-point the FTS index: drop the stale row at the old path,
                # index the note at the new one — else notes_search goes blind
                # to the moved note (re-introducing #830 for it).
                notes_index.index_note(conn, root, rel)  # old path now missing → drop
                notes_index.index_note(conn, root, new_rel)  # new path → (re)index
                stats["moved"] += 1
                pending += 1
                if pending >= chunk:
                    conn.commit()
                    pending = 0
                    log.info(
                        "engine.migrate_events_sharding.progress",
                        scanned=stats["scanned"],
                        moved=stats["moved"],
                        dry_run=dry_run,
                    )
        conn.commit()
    finally:
        conn.close()
    log.info("engine.migrate_events_sharding.done", dry_run=dry_run, **stats)
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report the plan without moving files or touching the db",
    )
    parser.add_argument(
        "--chunk", type=int, default=500, help="commit every N moves (default 500)"
    )
    args = parser.parse_args(argv)
    settings = Settings.from_env()
    stats = migrate(
        settings.solaris_db_path,
        settings.notes_dir,
        dry_run=args.dry_run,
        chunk=args.chunk,
    )
    print(  # noqa: T201 — a one-shot CLI, stdout is the report
        f"events-sharding {'DRY-RUN ' if args.dry_run else ''}"
        f"scanned={stats['scanned']} moved={stats['moved']} "
        f"skipped={stats['skipped']} no_year={stats['no_year']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

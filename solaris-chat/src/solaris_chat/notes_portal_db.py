"""DB-backed Notizen overview + stats (perf: no full-vault walk).

The `#/p/notes` overview and Statistik sections used to aggregate by walking the
whole vault (`_notes_overview_scan` / `_notes_stats_scan` in `server.py`): up to
20k `.md` files stat()'d + read on every cold portal open (~2s each on the ~79k-note
box). The engine already holds that data in `solaris.db` — the `fts_notes_meta`
FTS index (every note), the OKF projection (`entities`/`concepts`/`facts`/`events`),
and inline `mentions` — so these builders serve the SAME JSON shapes straight from
indexed queries instead.

Both builders return ``None`` when the projection is absent (a fresh install with
no engine DB, or the migration hasn't run): the caller then falls back to the
vault-scan path so nothing breaks. Owner scope mirrors the vault readers — a note
under ``users/<uid>/`` belongs to ``<uid>``, everything else is shared — applied as
a path-prefix filter on the indexed rows (no per-file read).
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from solaris_chat import mentions_store, notes_search

# The projection tables this path needs; absent → fall back to the vault scan.
_REQUIRED_TABLES = ("fts_notes_meta", "concepts", "entities")


def _connect(db_path: str) -> sqlite3.Connection | None:
    """A read-only-ish connection when the DB + projection tables exist, else None."""
    if not Path(db_path).exists():
        return None
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    try:
        have = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
            ).fetchall()
        }
    except sqlite3.OperationalError:
        conn.close()
        return None
    if not set(_REQUIRED_TABLES).issubset(have):
        conn.close()
        return None
    return conn


def _scope_clause(column: str, uid: str) -> tuple[str, list[str]]:
    """A path-prefix owner-scope SQL fragment for `column` (caller ∪ shared).

    A path under ``users/<other>/`` is another resident's private note and is
    excluded; ``users/<uid>/`` and everything outside ``users/`` is visible
    (default-deny, mirroring `notes_search.is_visible`)."""
    if uid == notes_search.SHARED_UID:
        return f"{column} NOT LIKE 'users/%'", []
    return f"({column} NOT LIKE 'users/%' OR {column} LIKE ?)", [f"users/{uid}/%"]


def _note_category(okf_path: str) -> str:
    """The folder/OKF domain for the categories breakdown (mirrors server's)."""
    parts = okf_path.replace("\\", "/").split("/")
    if parts[0] == "okf":
        return f"okf/{parts[1]}" if len(parts) > 2 else "okf"
    return parts[0] if len(parts) > 1 else "(Wurzel)"


def _stem_title(okf_path: str) -> str:
    """A display title from a path when we don't read the file: the stem."""
    name = okf_path.replace("\\", "/").rsplit("/", 1)[-1]
    return name[:-3] if name.endswith(".md") else name


def _month_series(counts: dict[str, int]) -> list[dict[str, Any]]:
    """A dense, gap-free last-12-months `[{month, count}]` series (as the scan)."""
    now = datetime.now(timezone.utc)
    series: list[dict[str, Any]] = []
    for i in range(11, -1, -1):
        y, m = divmod((now.year * 12 + now.month - 1) - i, 12)
        key = f"{y:04d}-{m + 1:02d}"
        series.append({"month": key, "count": counts.get(key, 0)})
    return series


def _top(counts: dict[str, int], top_n: int) -> list[dict[str, Any]]:
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [{"value": k, "count": v} for k, v in ranked[:top_n]]


def overview(
    db_path: str,
    uid: str,
    *,
    inbox_count: int,
    librarian: list[str],
) -> dict[str, Any] | None:
    """The `/api/portal/notes` overview from `solaris.db`, or None to fall back.

    `notes`/`facts` counts come from `fts_notes_meta` (path-scoped COUNTs), `recent`
    from the most-recently-`updated` `concepts` joined to their `okf_path` — no vault
    walk. `inbox`/`librarian` are the caller's bounded/cheap reads passed in."""
    conn = _connect(db_path)
    if conn is None:
        return None
    try:
        scope, params = _scope_clause("path", uid)
        total = conn.execute(
            f"SELECT COUNT(*) AS n FROM fts_notes_meta WHERE {scope}",  # noqa: S608
            params,
        ).fetchone()["n"]
        facts = conn.execute(
            "SELECT COUNT(*) AS n FROM fts_notes_meta"
            f" WHERE {scope} AND (path LIKE 'facts/%' OR path LIKE '%/facts/%')",  # noqa: S608
            params,
        ).fetchone()["n"]
        oscope, oparams = _scope_clause("okf_path", uid)
        rows = conn.execute(
            "SELECT okf_path FROM concepts"
            f" WHERE {oscope} ORDER BY updated DESC LIMIT 10",  # noqa: S608
            oparams,
        ).fetchall()
    finally:
        conn.close()
    recent = [
        {"path": r["okf_path"], "title": _stem_title(r["okf_path"])} for r in rows
    ]
    return {
        "ok": True,
        "counts": {"notes": total, "facts": facts, "inbox": inbox_count},
        "truncated": False,
        "librarian": librarian,
        "recent": recent,
    }


def stats(db_path: str, uid: str, top_n: int = 12) -> dict[str, Any] | None:
    """The `/api/portal/notes/stats` payload from `solaris.db`, or None to fall back.

    Tags/persons from inline `mentions`, categories + growth from `concepts`
    (`okf_path` folder + `updated` month), most-linked from `event_entities` edge
    counts — all indexed, no vault walk. Tags/persons are per-resident `mentions`
    (their own scope), so they need no path filter; the projection reads are
    path-scoped like the overview."""
    conn = _connect(db_path)
    if conn is None:
        return None
    try:
        scope, params = _scope_clause("path", uid)
        total = conn.execute(
            f"SELECT COUNT(*) AS n FROM fts_notes_meta WHERE {scope}",  # noqa: S608
            params,
        ).fetchone()["n"]
        oscope, oparams = _scope_clause("okf_path", uid)
        cat_rows = conn.execute(
            f"SELECT okf_path FROM concepts WHERE {oscope}",  # noqa: S608
            oparams,
        ).fetchall()
        month_rows = conn.execute(
            "SELECT substr(updated, 1, 7) AS m, COUNT(*) AS n FROM concepts"
            f" WHERE {oscope} GROUP BY m",  # noqa: S608
            oparams,
        ).fetchall()
        # Most-linked: participant entities by the number of events they appear in
        # (the projection's edge count, mirroring the vault's [[..]]-backlink rank).
        linked_rows = conn.execute(
            "SELECT en.canonical_name AS name, COUNT(*) AS n"
            " FROM event_entities ee JOIN entities en ON en.id = ee.entity_id"
            " WHERE en.resident_uid IN (?, ?)"
            " GROUP BY en.id ORDER BY n DESC, name LIMIT ?",
            (uid, notes_search.SHARED_UID, top_n),
        ).fetchall()
    finally:
        conn.close()

    categories: dict[str, int] = {}
    for r in cat_rows:
        cat = _note_category(r["okf_path"])
        categories[cat] = categories.get(cat, 0) + 1
    months = {r["m"]: r["n"] for r in month_rows if r["m"]}

    tags = mentions_store.counted_values(db_path, uid, mentions_store.KIND_TAG)
    persons = mentions_store.counted_values(db_path, uid, mentions_store.KIND_PERSON)

    return {
        "ok": True,
        "counts": {"notes": total},
        "truncated": False,
        "tags": _top(tags, top_n),
        "persons": _top(persons, top_n),
        "categories": _top(categories, top_n),
        "months": _month_series(months),
        "linked": [{"value": r["name"], "count": r["n"]} for r in linked_rows],
    }

"""SQLite access to the start-page favorites + their usage counter.

Reads/writes the `favorites` and `favorite_usage` tables (migration 0019) in
solaris.db — the operational state behind `pin_favorite` (#645). Same shape as
`topics_store`: sync sqlite3 (each op is millisecond-cheap), WAL + busy_timeout,
and reads degrade to empty when the DB or a table is missing (the schema-init
sidecar hasn't migrated yet), so the engine is safe to deploy before the
migration lands.

Scoping is per-resident: every row carries the owning `owner_uid`. A read for a
resident returns their own pins plus the shared `household` ones; writes always
target one explicit owner.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any

HOUSEHOLD = "household"


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def _payload_hash(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def list_favorites(db_path: str, uid: str) -> list[dict[str, Any]]:
    """The resident's pins plus the shared `household` ones, ordered by position.

    Empty when the DB/table is missing."""
    if not Path(db_path).exists():
        return []
    try:
        with _connect(db_path) as conn:
            rows = conn.execute(
                """
                SELECT id, owner_uid, kind, label, payload, position, created
                  FROM favorites
                 WHERE owner_uid = ? OR owner_uid = ?
                 ORDER BY position, created
                """,
                (uid, HOUSEHOLD),
            ).fetchall()
    except sqlite3.OperationalError:
        return []
    out: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        try:
            d["payload"] = json.loads(d["payload"])
        except (TypeError, ValueError):
            d["payload"] = {}
        out.append(d)
    return out


def add_favorite(
    db_path: str, owner_uid: str, kind: str, label: str, payload: dict[str, Any]
) -> str:
    """Append a favorite at the end of the owner's list; returns its id."""
    fav_id = uuid.uuid4().hex
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(position), -1) AS m FROM favorites WHERE owner_uid = ?",
            (owner_uid,),
        ).fetchone()
        position = int(row["m"]) + 1
        conn.execute(
            """
            INSERT INTO favorites (id, owner_uid, kind, label, payload, position)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                fav_id,
                owner_uid,
                kind,
                label,
                json.dumps(payload, ensure_ascii=False),
                position,
            ),
        )
        conn.commit()
    return fav_id


def remove_favorite(db_path: str, owner_uid: str, favorite_id: str) -> int:
    """Delete a favorite by id (owner-scoped); returns rows deleted."""
    with _connect(db_path) as conn:
        cur = conn.execute(
            "DELETE FROM favorites WHERE owner_uid = ? AND id = ?",
            (owner_uid, favorite_id),
        )
        conn.commit()
        return cur.rowcount


def remove_by_entity(db_path: str, owner_uid: str, entity_id: str) -> int:
    """Delete the owner's entity favorite(s) matching `entity_id`; rows deleted."""
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT id, payload FROM favorites WHERE owner_uid = ? AND kind = 'entity'",
            (owner_uid,),
        ).fetchall()
        ids = []
        for r in rows:
            try:
                payload = json.loads(r["payload"])
            except (TypeError, ValueError):
                continue
            if payload.get("entity_id") == entity_id:
                ids.append(r["id"])
        for fav_id in ids:
            conn.execute(
                "DELETE FROM favorites WHERE owner_uid = ? AND id = ?",
                (owner_uid, fav_id),
            )
        conn.commit()
        return len(ids)


def set_position(db_path: str, owner_uid: str, favorite_id: str, position: int) -> None:
    """Move a favorite to an explicit position (owner-scoped)."""
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE favorites SET position = ? WHERE owner_uid = ? AND id = ?",
            (position, owner_uid, favorite_id),
        )
        conn.commit()


def record_usage(db_path: str, owner_uid: str, tool: str, args: dict[str, Any]) -> None:
    """Increment the usage counter for one executed pinnable tool call."""
    payload = {"tool": tool, "args": args}
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO favorite_usage (owner_uid, kind, payload_hash, payload, count)
            VALUES (?, 'action', ?, ?, 1)
            ON CONFLICT(owner_uid, payload_hash)
            DO UPDATE SET count = count + 1, last_used = datetime('now')
            """,
            (
                owner_uid,
                _payload_hash(payload),
                json.dumps(payload, ensure_ascii=False),
            ),
        )
        conn.commit()


def top_usage(db_path: str, uid: str, limit: int = 5) -> list[dict[str, Any]]:
    """The resident's (+ household) most-used actions, most-frequent first.

    Empty when the DB/table is missing."""
    if not Path(db_path).exists():
        return []
    try:
        with _connect(db_path) as conn:
            rows = conn.execute(
                """
                SELECT owner_uid, kind, payload, count, last_used
                  FROM favorite_usage
                 WHERE owner_uid = ? OR owner_uid = ?
                 ORDER BY count DESC, last_used DESC
                 LIMIT ?
                """,
                (uid, HOUSEHOLD, limit),
            ).fetchall()
    except sqlite3.OperationalError:
        return []
    out: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        try:
            d["payload"] = json.loads(d["payload"])
        except (TypeError, ValueError):
            d["payload"] = {}
        out.append(d)
    return out

"""SQLite access to inline `#tag` / `@person` mentions.

Reads/writes the `mentions` table (migration 0006) in solaris.db. The proxy
parses `#tag` and `@person` tokens out of each user turn and records them here,
scoped to the resident (#279, solaris-architecture.md §3). The tag-cloud reads a
session's mentions; the autosuggest endpoints read a resident's known tags /
persons.

Sync sqlite3, like `topics_store` / the gatekeeper's `rooms_store`: each op is
millisecond-cheap. If solaris.db or the table is missing (the schema-init
sidecar hasn't migrated yet), reads degrade to empty rather than erroring — the
cloud/autosuggest just show nothing until the migration lands.

Scoping is per-resident (D3): every row carries the recording `owner_uid` and
every read/write is filtered by it, so a resident never sees another resident's
mentions.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

KIND_TAG = "tag"
KIND_PERSON = "person"


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def record_mentions(
    db_path: str,
    session_id: str,
    owner_uid: str,
    tags: list[str],
    persons: list[str],
) -> int | None:
    """Record a turn's `#tag` / `@person` mentions; return the message_ref used.

    All mentions from one turn share one `message_ref` — the next per-session
    ordinal (`MAX(message_ref) + 1`), so they group to the same message bubble
    for jump-to-message (#279c). No-op (returns None) when there's nothing to
    record, the DB is missing, or the table hasn't been migrated yet.
    """
    if not (tags or persons) or not Path(db_path).exists():
        return None
    rows = [(KIND_TAG, v) for v in tags] + [(KIND_PERSON, v) for v in persons]
    try:
        with _connect(db_path) as conn:
            ref = conn.execute(
                "SELECT COALESCE(MAX(message_ref), -1) + 1 AS next "
                "FROM mentions WHERE session_id = ? AND owner_uid = ?",
                (session_id, owner_uid),
            ).fetchone()["next"]
            conn.executemany(
                """
                INSERT INTO mentions (session_id, message_ref, kind, value, owner_uid)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(session_id, message_ref, kind, value) DO NOTHING
                """,
                [(session_id, ref, kind, value, owner_uid) for kind, value in rows],
            )
            conn.commit()
    except sqlite3.OperationalError:
        return None
    return ref


def list_session_mentions(
    db_path: str, session_id: str, owner_uid: str
) -> list[dict[str, Any]]:
    """The resident's mentions for a chat (tag-cloud, #279c).

    Each item is `{kind, value, message_ref}`, ordered by first appearance.
    Empty when the DB/table is missing or the chat has none.
    """
    if not Path(db_path).exists():
        return []
    try:
        with _connect(db_path) as conn:
            rows = conn.execute(
                """
                SELECT kind, value, MIN(message_ref) AS message_ref
                  FROM mentions
                 WHERE session_id = ? AND owner_uid = ?
                 GROUP BY kind, value
                 ORDER BY message_ref, kind, value
                """,
                (session_id, owner_uid),
            ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [dict(r) for r in rows]


def known_tags_for(db_path: str, owner_uid: str, prefix: str = "") -> list[str]:
    """Distinct tags the resident has used, for autosuggest (prefix-filtered)."""
    return _known_values(db_path, owner_uid, KIND_TAG, prefix)


def known_persons_for(db_path: str, owner_uid: str, prefix: str = "") -> list[str]:
    """Distinct persons the resident has mentioned, for autosuggest.

    Seeded persons (residents/registry + a manual list) live in the server, not
    here — this returns only persons the resident has actually used in chat. The
    endpoint unions the two.
    """
    return _known_values(db_path, owner_uid, KIND_PERSON, prefix)


def _known_values(db_path: str, owner_uid: str, kind: str, prefix: str) -> list[str]:
    if not Path(db_path).exists():
        return []
    sql = "SELECT DISTINCT value FROM mentions WHERE owner_uid = ? AND kind = ?"
    params: list[str] = [owner_uid, kind]
    if prefix:
        sql += " AND value LIKE ? ESCAPE '\\'"
        params.append(_like_prefix(prefix))
    sql += " ORDER BY value"
    try:
        with _connect(db_path) as conn:
            rows = conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError:
        return []
    return [r["value"] for r in rows]


def backlinks_for(
    db_path: str, owner_uid: str, values: list[str], limit: int = 20
) -> list[dict[str, Any]]:
    """Chat turns where the resident mentioned any of `values` (concept page).

    A backlink is `{session_id, message_ref, value}` — the turn that referenced
    the concept, for the #502 page's "mentioned in chat" list. Case-insensitive
    on the recorded `#tag`/`@person` value. Empty when DB/table missing or none.
    """
    vals = [v for v in dict.fromkeys(values) if v]
    if not vals or not Path(db_path).exists():
        return []
    placeholders = ",".join("?" for _ in vals)
    try:
        with _connect(db_path) as conn:
            rows = conn.execute(
                f"""
                SELECT session_id, message_ref, value
                  FROM mentions
                 WHERE owner_uid = ?
                   AND value COLLATE NOCASE IN ({placeholders})
                 ORDER BY session_id, message_ref
                 LIMIT ?
                """,
                (owner_uid, *vals, limit),
            ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [dict(r) for r in rows]


def _like_prefix(prefix: str) -> str:
    """Escape LIKE wildcards so a literal prefix match isn't a glob."""
    escaped = prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return escaped + "%"

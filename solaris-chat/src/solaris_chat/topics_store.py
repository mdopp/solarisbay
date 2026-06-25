"""SQLite access to the topics registry and chat<->topic assignments.

Reads the `topics` table (registry, migration 0004) and reads/writes
`session_topics` (chat<->topic assignment, migration 0005) in solaris.db. The
proxy is otherwise stateless; this is the one store it owns for the Topics
feature (the assignment + display surfaces, #241).

Sync sqlite3, like the gatekeeper's `rooms_store`: each op is millisecond-cheap.
If solaris.db or a table is missing (the schema-init sidecar hasn't migrated
yet), reads degrade to empty rather than erroring — the picker just shows no
topics until the migration lands.

Scoping is per-resident (D3): assignments carry the assigning `owner_uid` and
every read/write is filtered by it, so a resident never sees or mutates another
resident's chat<->topic rows.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    # WAL + busy_timeout so concurrent writers wait instead of raising
    # "database is locked" (#600).
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def list_topics(db_path: str, owner_uid: str) -> list[dict[str, Any]]:
    """Topics the resident may assign: their own plus shared/admin/system ones.

    System topics (null owner_uid) and shared/admin-scoped topics are visible to
    every resident; resident-scoped topics only to their owner. Archived topics
    are omitted. Empty when the DB/table is missing.
    """
    if not Path(db_path).exists():
        return []
    try:
        with _connect(db_path) as conn:
            rows = conn.execute(
                """
                SELECT slug, display_name, parent, scope, color
                  FROM topics
                 WHERE archived = 0
                   AND (owner_uid = ? OR owner_uid IS NULL OR scope != 'resident')
                 ORDER BY display_name
                """,
                (owner_uid,),
            ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [dict(r) for r in rows]


def get_session_topics(db_path: str, session_id: str, owner_uid: str) -> dict[str, Any]:
    """The resident's topics for a chat: `{primary: slug|None, secondary: [..]}`.

    Empty (`{"primary": None, "secondary": []}`) when the DB/table is missing or
    the chat has no assignment.
    """
    empty: dict[str, Any] = {"primary": None, "secondary": []}
    if not Path(db_path).exists():
        return empty
    try:
        with _connect(db_path) as conn:
            rows = conn.execute(
                """
                SELECT topic_slug, role
                  FROM session_topics
                 WHERE session_id = ? AND owner_uid = ?
                """,
                (session_id, owner_uid),
            ).fetchall()
    except sqlite3.OperationalError:
        return empty
    primary: str | None = None
    secondary: list[str] = []
    for r in rows:
        if r["role"] == "primary":
            primary = r["topic_slug"]
        else:
            secondary.append(r["topic_slug"])
    secondary.sort()
    return {"primary": primary, "secondary": secondary}


def topic_defaults(db_path: str, slug: str) -> dict[str, str | None]:
    """A topic's `{default_model, default_persona}` (D2), both possibly None.

    Used at session create to bind the topic's model + persona (#242). Returns
    `{"default_model": None, "default_persona": None}` when the DB/table/row is
    missing — the caller then falls back to the normal routing/persona.
    """
    empty: dict[str, str | None] = {"default_model": None, "default_persona": None}
    if not slug or not Path(db_path).exists():
        return empty
    try:
        with _connect(db_path) as conn:
            row = conn.execute(
                "SELECT default_model, default_persona FROM topics WHERE slug = ?",
                (slug,),
            ).fetchone()
    except sqlite3.OperationalError:
        return empty
    if row is None:
        return empty
    return {
        "default_model": row["default_model"],
        "default_persona": row["default_persona"],
    }


def topic_context_hint(db_path: str, session_id: str, owner_uid: str) -> str | None:
    """A machine-readable system-context line for the chat's primary topic.

    The proxy prepends this to each turn the agent sees so any ingestion skill
    running in the turn knows which topic to stamp (`#topic/<slug>`, the
    architecture's data->topic tagging convention). Returns None when the chat
    has no primary topic (non-topic chats stay untouched) or the DB is missing.

    Shape: `[Active topic: <display_name> #topic/<slug>]` — the `#topic/<slug>`
    token is what the ingestion skills read; the display name is for the model's
    benefit. The slug may be hierarchical (e.g. `projekt/wintergarten`).
    """
    assigned = get_session_topics(db_path, session_id, owner_uid)
    slug = assigned["primary"]
    if not slug:
        return None
    display = _display_name(db_path, slug) or slug
    return f"[Active topic: {display} #topic/{slug}]"


def display_name(db_path: str, slug: str) -> str | None:
    """The topic's display_name for `slug`, or None when DB/table/row is missing.

    Used by the incognito extract path (#246) to label the chosen topic; the
    extract turn supplies a raw slug, so the proxy resolves the human name here.
    """
    return _display_name(db_path, slug)


def _display_name(db_path: str, slug: str) -> str | None:
    """The topic's display_name, or None when the DB/table/row is missing."""
    if not slug or not Path(db_path).exists():
        return None
    try:
        with _connect(db_path) as conn:
            row = conn.execute(
                "SELECT display_name FROM topics WHERE slug = ?", (slug,)
            ).fetchone()
    except sqlite3.OperationalError:
        return None
    return row["display_name"] if row else None


def primary_topics_for(
    db_path: str, session_ids: list[str], owner_uid: str
) -> dict[str, str]:
    """`{session_id: primary_slug}` for the resident's chats (chip rendering).

    Only sessions with a primary assignment appear. Empty when DB/table missing.
    """
    if not session_ids or not Path(db_path).exists():
        return {}
    placeholders = ",".join("?" for _ in session_ids)
    try:
        with _connect(db_path) as conn:
            rows = conn.execute(
                f"""
                SELECT session_id, topic_slug
                  FROM session_topics
                 WHERE role = 'primary' AND owner_uid = ?
                   AND session_id IN ({placeholders})
                """,
                (owner_uid, *session_ids),
            ).fetchall()
    except sqlite3.OperationalError:
        return {}
    return {r["session_id"]: r["topic_slug"] for r in rows}


def create_topic(
    db_path: str,
    slug: str,
    display_name: str,
    owner_uid: str,
    color: str | None = None,
) -> None:
    """Create a resident-scoped topic row from a confirmed suggestion (D4, #245).

    Idempotent: a slug that already exists is left untouched (a re-confirmed
    suggestion, or a slug the resident created manually, must not clobber the
    existing display_name/color). The topic suggester only ever creates the
    resident's own topics, so scope is fixed to `resident` and owner_uid is the
    confirming resident.
    """
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO topics (slug, display_name, scope, owner_uid, color)
            VALUES (?, ?, 'resident', ?, ?)
            ON CONFLICT(slug) DO NOTHING
            """,
            (slug, display_name, owner_uid, color),
        )
        conn.commit()


def set_primary(db_path: str, session_id: str, slug: str, owner_uid: str) -> None:
    """Set (replace) the chat's single primary topic for this resident (D1)."""
    with _connect(db_path) as conn:
        conn.execute(
            "DELETE FROM session_topics "
            "WHERE session_id = ? AND owner_uid = ? AND role = 'primary'",
            (session_id, owner_uid),
        )
        # A slug demoted from secondary->primary would collide on the PK; clear
        # any existing row for this (session, slug) first.
        conn.execute(
            "DELETE FROM session_topics "
            "WHERE session_id = ? AND owner_uid = ? AND topic_slug = ?",
            (session_id, owner_uid, slug),
        )
        conn.execute(
            "INSERT INTO session_topics (session_id, topic_slug, role, owner_uid) "
            "VALUES (?, ?, 'primary', ?)",
            (session_id, slug, owner_uid),
        )
        conn.commit()


def add_secondary(db_path: str, session_id: str, slug: str, owner_uid: str) -> None:
    """Add a secondary tag to the chat (idempotent; no-op if already present)."""
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO session_topics (session_id, topic_slug, role, owner_uid)
            VALUES (?, ?, 'secondary', ?)
            ON CONFLICT(session_id, topic_slug) DO NOTHING
            """,
            (session_id, slug, owner_uid),
        )
        conn.commit()


def remove_topic(db_path: str, session_id: str, slug: str, owner_uid: str) -> None:
    """Remove a topic (primary or secondary) from the chat for this resident."""
    with _connect(db_path) as conn:
        conn.execute(
            "DELETE FROM session_topics "
            "WHERE session_id = ? AND owner_uid = ? AND topic_slug = ?",
            (session_id, owner_uid, slug),
        )
        conn.commit()

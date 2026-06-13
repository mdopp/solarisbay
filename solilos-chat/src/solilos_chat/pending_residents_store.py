"""SQLite access to pending resident-registration requests.

Reads/writes the `pending_residents` table (migration 0013) in solilos.db. The
onboarding registration flow (#376) writes a row here after the gatekeeper has
enrolled a guest's voice; the admin-approval step (#355, separate) reads the
pending rows and flips their status. A pending row is **not** an account — it is
only the local record of a request awaiting approval.

Sync sqlite3, like `topics_store` / `mentions_store`: each op is millisecond-
cheap. If solilos.db or the table is missing (the schema-init sidecar hasn't
migrated yet), a read degrades to empty and a write raises so the registration
tool can surface the failure rather than silently dropping the request.

Not per-resident scoped: a candidate has no resident uid of their own yet (only
the one they're asking for), so a pending request belongs to the household. The
biometric audio never reaches this table — only the candidate uid/name and
whether enrolment succeeded.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

STATUS_PENDING = "pending"


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def add_pending_resident(
    db_path: str, uid: str, display_name: str, enrolled: bool
) -> int:
    """Record a registration request awaiting admin approval; return its row id."""
    with _connect(db_path) as conn:
        cur = conn.execute(
            """
            INSERT INTO pending_residents (uid, display_name, status, enrolled)
            VALUES (?, ?, ?, ?)
            """,
            (uid, display_name, STATUS_PENDING, 1 if enrolled else 0),
        )
        conn.commit()
        return int(cur.lastrowid)


def list_pending_residents(db_path: str) -> list[dict[str, Any]]:
    """The open registration requests, newest first (the #355 approval surface).

    Empty when the DB/table is missing.
    """
    if not Path(db_path).exists():
        return []
    try:
        with _connect(db_path) as conn:
            rows = conn.execute(
                """
                SELECT id, uid, display_name, status, enrolled, requested_at
                  FROM pending_residents
                 WHERE status = ?
                 ORDER BY requested_at DESC, id DESC
                """,
                (STATUS_PENDING,),
            ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [dict(r) for r in rows]

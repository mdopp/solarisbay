"""SQLite-backed satellite->room mapping for Solilos voice control.

The `voice_pe_rooms` table (`satellite_id` PK -> `room`) is provisioned by
the alembic migration `0003_voice_pe_rooms`. The gatekeeper reads it on
each turn to attach `location` to the Hermes payload; the `POST /room`
endpoint writes it (rooms are self-enrolled by conversation — see #94).

Sync sqlite3, like `embeddings_store`: each op is millisecond-cheap and the
table holds one row per satellite. If solilos.db or the table is missing
(init container hasn't migrated yet), reads return None/{} and callers
degrade to "room unknown".

Interim store: the longer-term goal is to source device->area from Home
Assistant as the single source of truth.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def get_room(db_path: str, satellite_id: str) -> str | None:
    """Room for a satellite, or None when unknown / DB not ready."""
    if not satellite_id or not Path(db_path).exists():
        return None
    try:
        with _connect(db_path) as conn:
            row = conn.execute(
                "SELECT room FROM voice_pe_rooms WHERE satellite_id = ?",
                (satellite_id,),
            ).fetchone()
    except sqlite3.OperationalError:
        return None
    return str(row["room"]) if row else None


def set_room(db_path: str, satellite_id: str, room: str) -> None:
    """Insert or remap the room for a satellite."""
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO voice_pe_rooms (satellite_id, room)
            VALUES (?, ?)
            ON CONFLICT(satellite_id) DO UPDATE SET
                room       = excluded.room,
                updated_at = datetime('now')
            """,
            (satellite_id, room),
        )
        conn.commit()


def list_rooms(db_path: str) -> dict[str, str]:
    """All satellite->room mappings (empty when DB/table missing)."""
    if not Path(db_path).exists():
        return {}
    try:
        with _connect(db_path) as conn:
            rows = conn.execute(
                "SELECT satellite_id, room FROM voice_pe_rooms ORDER BY satellite_id"
            ).fetchall()
    except sqlite3.OperationalError:
        return {}
    return {str(r["satellite_id"]): str(r["room"]) for r in rows}


def delete_room(db_path: str, satellite_id: str) -> bool:
    """Remove a satellite's mapping. Returns whether a row was deleted."""
    if not Path(db_path).exists():
        return False
    try:
        with _connect(db_path) as conn:
            cur = conn.execute(
                "DELETE FROM voice_pe_rooms WHERE satellite_id = ?", (satellite_id,)
            )
            conn.commit()
            return cur.rowcount > 0
    except sqlite3.OperationalError:
        return False

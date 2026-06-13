"""Facade-side reader for the transcript-keyed uid side-channel (#350).

The gatekeeper, acting as HA's Wyoming STT provider, resolves the speaking
resident and writes `{transcript -> uid}` into `solilos.db.voice_uid_stash`
(see `gatekeeper/uid_stash.py`). HA then calls the engine facade
(`conversation.sol`) with the same transcript as the latest user message but
no uid. This module looks the uid up by that transcript so the spoken turn
is attributed to the right resident.

Consume-once + short TTL: a lookup deletes the row (so a later turn with the
same utterance never re-reads a stale identity) and ignores rows older than
the TTL (so a transcript that never reached the facade — e.g. HA dropped the
turn — can't attribute a much-later identical utterance). On any miss the
caller falls back to `household`.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

# A spoken turn flows STT -> conversation within a couple of seconds; 30s is
# generously above that and well below the gap to an unrelated later turn.
STASH_TTL_SECONDS = 30


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def consume_uid(db_path: str, transcript: str) -> str | None:
    """Return the resident uid the gatekeeper stashed for this transcript, or
    None on a miss/expiry. Consume-once: a fresh hit is deleted so it can't be
    re-read by a later identical utterance. Best-effort — a missing table/DB
    returns None and the caller falls back to the default uid."""
    if not transcript or not Path(db_path).exists():
        return None
    try:
        with _connect(db_path) as conn:
            row = conn.execute(
                """
                SELECT uid FROM voice_uid_stash
                WHERE transcript = ?
                  AND created_at >= datetime('now', ?)
                """,
                (transcript, f"-{STASH_TTL_SECONDS} seconds"),
            ).fetchone()
            # Delete unconditionally on a keyed lookup: a hit is consumed, and
            # an expired row is reaped so the table can't grow unbounded.
            conn.execute(
                "DELETE FROM voice_uid_stash WHERE transcript = ?", (transcript,)
            )
            conn.commit()
    except sqlite3.OperationalError:
        return None
    return str(row["uid"]) if row else None

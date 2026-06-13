"""Transcript-keyed uid side-channel for the live HA Assist path (#350).

When the gatekeeper serves as HA's Wyoming STT provider it transcribes the
turn AND resolves the speaking resident (ECAPA + k-NN), but HA — not the
gatekeeper — runs the conversation step. HA forwards only the transcript
text to the engine facade (`conversation.sol`), with no uid. So the
gatekeeper stashes `{transcript -> uid}` here; the facade reads it back by
the incoming utterance text to attribute the spoken turn to the resident.

The transcript is the shared correlation key: the gatekeeper produced it and
the facade receives the identical string a moment later. Consume-once + a
short TTL bound the only failure mode — a stale or collided uid never leaks
into a later turn.

Sync sqlite3 over the same `solilos.db` the rest of the gatekeeper opens
(`rooms_store`, `embeddings_store`). The table is provisioned by alembic
migration `0012_voice_uid_stash`; if it's missing (init container hasn't
migrated yet) the writer no-ops so the STT path keeps working.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def stash_uid(db_path: str, transcript: str, uid: str) -> None:
    """Record `{transcript -> uid}` for the facade to consume on the next
    turn. Best-effort: a missing table/DB (init container not yet migrated)
    must not break the STT response, so failures are swallowed."""
    if not transcript or not Path(db_path).exists():
        return
    try:
        with _connect(db_path) as conn:
            conn.execute(
                """
                INSERT INTO voice_uid_stash (transcript, uid, created_at)
                VALUES (?, ?, datetime('now'))
                ON CONFLICT(transcript) DO UPDATE SET
                    uid        = excluded.uid,
                    created_at = excluded.created_at
                """,
                (transcript, uid),
            )
            conn.commit()
    except sqlite3.OperationalError:
        return

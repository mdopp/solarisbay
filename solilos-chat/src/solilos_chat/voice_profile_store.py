"""SQLite access to per-resident voice profiles (speaker embeddings).

Reads/writes the `voice_profiles` table (migration 0012) in solilos.db. A voice
profile is a resident's speaker embedding plus its enrollment metadata, used
later by the speaker matcher (#350) and onboarding enrollment (#354). This module
owns the enrollment lifecycle only — enroll / re-enroll / delete / lookup — not
any matching logic.

Sync sqlite3, like `trace_store` / `mentions_store`: each op is
millisecond-cheap. If solilos.db or the table is missing (the schema-init
sidecar hasn't migrated yet), reads degrade to None/empty and writes raise
`OperationalError` only if the caller chooses to surface it — here writes also
no-op when the DB file is absent, mirroring the sibling stores.

Privacy: a speaker embedding is biometric data. It is kept local-only (never
leaves the box), scoped per resident by `owner_uid`, and hard-deletable on
request (`delete_voice_profile` removes the row outright — no soft-delete, no
tombstone). `owner_uid` is the primary key: one current profile per resident,
re-enrollment replaces the embedding in place while preserving `enrolled_at`.

Note for the reviewer: the baseline migration (0001) also provisions a
Hermes-era `voice_embeddings` table with a gatekeeper-side store
(`voice-gatekeeper/.../embeddings_store.py`). This Sol Engine store is the
post-Hermes home for the enrollment lifecycle consumed by #350/#354 (both Sol
Engine units). Whether the two tables should be unified is a design call for the
draft review — see the PR body.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def enroll_voice_profile(
    db_path: str,
    owner_uid: str,
    embedding: bytes,
    enroll_count: int,
) -> None:
    """Enroll or re-enroll a resident's voice profile.

    `embedding` is the speaker vector derived from `enroll_count` utterances
    (the derivation lives in the enrollment flow, #354 — not here). First enroll
    inserts; a later call for the same `owner_uid` re-enrolls, replacing the
    embedding/count and bumping `updated_at` while keeping the original
    `enrolled_at`. No-op when the DB file is missing (the migration hasn't run).
    """
    if not Path(db_path).exists():
        return
    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO voice_profiles (owner_uid, embedding, enroll_count)
            VALUES (?, ?, ?)
            ON CONFLICT(owner_uid) DO UPDATE SET
              embedding    = excluded.embedding,
              enroll_count = excluded.enroll_count,
              updated_at   = datetime('now')
            """,
            (owner_uid, embedding, enroll_count),
        )
        conn.commit()


def get_voice_profile(db_path: str, owner_uid: str) -> dict[str, Any] | None:
    """The resident's current voice profile, or None if not enrolled.

    Returns `{owner_uid, embedding, enroll_count, enrolled_at, updated_at}`.
    Scoped to `owner_uid`: a resident only ever reads their own profile. None
    when the DB/table is missing or the resident has no profile.
    """
    if not Path(db_path).exists():
        return None
    try:
        with _connect(db_path) as conn:
            row = conn.execute(
                """
                SELECT owner_uid, embedding, enroll_count, enrolled_at, updated_at
                  FROM voice_profiles
                 WHERE owner_uid = ?
                """,
                (owner_uid,),
            ).fetchone()
    except sqlite3.OperationalError:
        return None
    return dict(row) if row is not None else None


def delete_voice_profile(db_path: str, owner_uid: str) -> bool:
    """Hard-delete a resident's voice profile on request; True if a row was removed.

    Biometric data is deletable on request: this removes the row outright. False
    when the DB/table is missing or the resident had no profile.
    """
    if not Path(db_path).exists():
        return False
    try:
        with _connect(db_path) as conn:
            cur = conn.execute(
                "DELETE FROM voice_profiles WHERE owner_uid = ?",
                (owner_uid,),
            )
            conn.commit()
            return cur.rowcount > 0
    except sqlite3.OperationalError:
        return False

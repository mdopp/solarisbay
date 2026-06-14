"""Tests for the gatekeeper-side uid stash writer (#350)."""

from __future__ import annotations

import sqlite3

from gatekeeper.uid_stash import stash_uid


_SCHEMA = """
CREATE TABLE voice_uid_stash (
    transcript TEXT PRIMARY KEY,
    uid        TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def _db(tmp_path) -> str:
    path = str(tmp_path / "solaris.db")
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA)
    conn.commit()
    conn.close()
    return path


def _read(db: str, transcript: str):
    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT uid FROM voice_uid_stash WHERE transcript = ?", (transcript,)
    ).fetchone()
    conn.close()
    return row[0] if row else None


def test_stash_writes_row(tmp_path):
    db = _db(tmp_path)
    stash_uid(db, "licht an", "anna")
    assert _read(db, "licht an") == "anna"


def test_stash_upserts_latest_uid(tmp_path):
    db = _db(tmp_path)
    stash_uid(db, "licht an", "anna")
    stash_uid(db, "licht an", "michael")
    assert _read(db, "licht an") == "michael"


def test_stash_empty_transcript_is_noop(tmp_path):
    db = _db(tmp_path)
    stash_uid(db, "", "anna")
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM voice_uid_stash").fetchone()[0] == 0
    conn.close()


def test_stash_missing_db_is_noop(tmp_path):
    # No DB file yet (init container hasn't migrated) — must not raise.
    stash_uid(str(tmp_path / "absent.db"), "licht an", "anna")


def test_stash_missing_table_is_noop(tmp_path):
    path = str(tmp_path / "solaris.db")
    sqlite3.connect(path).close()  # DB exists, table doesn't
    stash_uid(path, "licht an", "anna")  # OperationalError swallowed

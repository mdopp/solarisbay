"""Tests for the per-resident voice-profile store (#349).

Covers the `voice_profiles` store enrollment lifecycle: enroll round-trip,
re-enroll replaces the embedding while preserving `enrolled_at`, per-resident
scope, hard delete, and missing-db degradation. No speaker-matching logic — that
is #350.
"""

from __future__ import annotations

import sqlite3

from solilos_chat import voice_profile_store

# The schema the 0012 migration creates, replayed locally so the store tests run
# against a real sqlite db without alembic.
_SCHEMA = """
CREATE TABLE voice_profiles (
  owner_uid     TEXT NOT NULL PRIMARY KEY,
  embedding     BLOB NOT NULL,
  enroll_count  INTEGER NOT NULL,
  enrolled_at   TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def _db(tmp_path) -> str:
    path = str(tmp_path / "solilos.db")
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA)
    conn.commit()
    conn.close()
    return path


def test_enroll_then_get_round_trips(tmp_path):
    db = _db(tmp_path)
    emb = b"\x01\x02\x03\x04"
    voice_profile_store.enroll_voice_profile(db, "mdopp", emb, enroll_count=5)
    got = voice_profile_store.get_voice_profile(db, "mdopp")
    assert got is not None
    assert got["owner_uid"] == "mdopp"
    assert got["embedding"] == emb
    assert got["enroll_count"] == 5
    assert got["enrolled_at"]
    assert got["updated_at"]


def test_get_unenrolled_is_none(tmp_path):
    db = _db(tmp_path)
    assert voice_profile_store.get_voice_profile(db, "lena") is None


def test_reenroll_replaces_embedding_and_keeps_enrolled_at(tmp_path):
    db = _db(tmp_path)
    voice_profile_store.enroll_voice_profile(db, "mdopp", b"old", enroll_count=3)
    first = voice_profile_store.get_voice_profile(db, "mdopp")

    voice_profile_store.enroll_voice_profile(db, "mdopp", b"new-vector", enroll_count=8)
    got = voice_profile_store.get_voice_profile(db, "mdopp")
    assert got["embedding"] == b"new-vector"
    assert got["enroll_count"] == 8
    # Re-enroll preserves the original enrollment timestamp, replaces in place
    # (still exactly one profile for the resident).
    assert got["enrolled_at"] == first["enrolled_at"]
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM voice_profiles").fetchone()[0] == 1


def test_scopes_to_resident(tmp_path):
    db = _db(tmp_path)
    voice_profile_store.enroll_voice_profile(db, "mdopp", b"m", enroll_count=2)
    voice_profile_store.enroll_voice_profile(db, "lena", b"l", enroll_count=4)
    assert voice_profile_store.get_voice_profile(db, "mdopp")["embedding"] == b"m"
    assert voice_profile_store.get_voice_profile(db, "lena")["embedding"] == b"l"


def test_delete_removes_profile_on_request(tmp_path):
    db = _db(tmp_path)
    voice_profile_store.enroll_voice_profile(db, "mdopp", b"m", enroll_count=2)
    assert voice_profile_store.delete_voice_profile(db, "mdopp") is True
    assert voice_profile_store.get_voice_profile(db, "mdopp") is None
    # Idempotent: deleting an absent profile reports no row removed.
    assert voice_profile_store.delete_voice_profile(db, "mdopp") is False


def test_delete_is_scoped_to_resident(tmp_path):
    db = _db(tmp_path)
    voice_profile_store.enroll_voice_profile(db, "mdopp", b"m", enroll_count=2)
    voice_profile_store.enroll_voice_profile(db, "lena", b"l", enroll_count=2)
    voice_profile_store.delete_voice_profile(db, "mdopp")
    # Deleting one resident's profile leaves the other's intact.
    assert voice_profile_store.get_voice_profile(db, "lena") is not None


def test_missing_db_degrades(tmp_path):
    missing = str(tmp_path / "absent.db")
    # No-op write, None read, False delete — never raises.
    voice_profile_store.enroll_voice_profile(missing, "mdopp", b"m", enroll_count=1)
    assert voice_profile_store.get_voice_profile(missing, "mdopp") is None
    assert voice_profile_store.delete_voice_profile(missing, "mdopp") is False

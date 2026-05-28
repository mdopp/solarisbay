"""Tests for the satellite->room store."""

from __future__ import annotations

import sqlite3

import pytest

from gatekeeper.rooms_store import delete_room, get_room, list_rooms, set_room

_DDL = """
CREATE TABLE voice_pe_rooms (
    satellite_id TEXT PRIMARY KEY,
    room         TEXT NOT NULL,
    updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
)
"""


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "oscar.db")
    conn = sqlite3.connect(path)
    conn.execute(_DDL)
    conn.commit()
    conn.close()
    return path


def test_set_and_get_room(db):
    set_room(db, "192.168.178.42", "kitchen")
    assert get_room(db, "192.168.178.42") == "kitchen"


def test_get_unknown_returns_none(db):
    assert get_room(db, "10.0.0.9") is None


def test_set_room_remaps(db):
    set_room(db, "192.168.178.42", "kitchen")
    set_room(db, "192.168.178.42", "bath")
    assert get_room(db, "192.168.178.42") == "bath"


def test_list_rooms(db):
    set_room(db, "a", "kitchen")
    set_room(db, "b", "office")
    assert list_rooms(db) == {"a": "kitchen", "b": "office"}


def test_delete_room(db):
    set_room(db, "a", "kitchen")
    assert delete_room(db, "a") is True
    assert get_room(db, "a") is None
    assert delete_room(db, "a") is False


def test_get_room_missing_db_returns_none(tmp_path):
    assert get_room(str(tmp_path / "nope.db"), "a") is None


def test_get_room_empty_satellite_id_returns_none(db):
    assert get_room(db, "") is None

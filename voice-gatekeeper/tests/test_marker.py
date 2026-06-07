"""Tests for the voice-session uid marker (#153)."""

from __future__ import annotations

import re

from gatekeeper import marker


def test_uid_hash_is_8_hex_and_stable():
    h = marker.uid_hash("household")
    assert re.fullmatch(r"[0-9a-f]{8}", h)
    assert marker.uid_hash("household") == h  # deterministic


def test_marker_for_format_and_does_not_leak_username():
    m = marker.marker_for("michael")
    assert re.fullmatch(r"\[uid:[0-9a-f]{8}\] ", m)
    assert "michael" not in m  # raw username is hashed, never embedded


def test_distinct_uids_get_distinct_markers():
    assert marker.marker_for("household") != marker.marker_for("michael")

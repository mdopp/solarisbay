"""Tests for the per-resident uid title marker (#153)."""

from __future__ import annotations

import re

from solaris_chat import marker


def test_uid_hash_is_8_hex_and_stable():
    h = marker.uid_hash("mdopp")
    assert re.fullmatch(r"[0-9a-f]{8}", h)
    assert marker.uid_hash("mdopp") == h


def test_marker_for_format_and_does_not_leak_username():
    m = marker.marker_for("mdopp")
    assert re.fullmatch(r"\[uid:[0-9a-f]{8}\] ", m)
    assert "mdopp" not in m  # username is hashed, never embedded raw


def test_embed_prefixes_and_stays_under_hermes_limit():
    out = marker.embed("mdopp", "Plan the summer trip")
    assert out.startswith(marker.marker_for("mdopp"))
    assert out.endswith("Plan the summer trip")
    long = "x" * 60
    assert len(marker.embed("mdopp", long)) < 100  # Hermes' 100-char title cap


def test_embed_is_idempotent_no_double_marker():
    once = marker.embed("mdopp", "Groceries")
    twice = marker.embed("mdopp", once)
    assert twice == once  # re-embedding swaps, never stacks


def test_embed_reembed_changes_owner():
    a = marker.embed("mdopp", "Shared title")
    b = marker.embed("lena", a)
    assert b == marker.embed("lena", "Shared title")
    assert not marker.has_marker("mdopp", b)
    assert marker.has_marker("lena", b)


def test_strip_removes_only_the_marker():
    assert marker.strip(marker.embed("mdopp", "Hello world")) == "Hello world"
    # No marker -> unchanged.
    assert marker.strip("just a title") == "just a title"
    # A bracket that isn't a marker is left intact.
    assert marker.strip("[note] keep me") == "[note] keep me"


def test_has_marker_owner_other_and_legacy():
    tagged = marker.embed("mdopp", "mine")
    assert marker.has_marker("mdopp", tagged) is True
    assert marker.has_marker("lena", tagged) is False  # different resident
    assert marker.has_marker("mdopp", "untagged legacy") is False  # no marker hidden
    assert marker.has_marker("mdopp", "") is False


# --- Maintenance marker (#229) --------------------------------------------


def test_maint_marker_format_and_does_not_leak_username():
    m = marker.maint_marker("mdopp")
    assert re.fullmatch(r"\[maint:[0-9a-f]{8}\] ", m)
    assert "mdopp" not in m
    # Same uid hash as the household marker, distinct namespace prefix.
    assert m == f"[maint:{marker.uid_hash('mdopp')}] "


def test_maint_marker_is_hidden_from_household_filter():
    # A maintenance-tagged title must never match the household `[uid:...]`
    # filter, so maintenance sessions stay out of the resident session list.
    maint_title = marker.maint_marker("mdopp") + "diagnostics"
    assert marker.has_marker("mdopp", maint_title) is False
    # And a household title is not mistaken for a maintenance one.
    assert not marker.maint_marker("mdopp").startswith(marker.marker_for("mdopp"))


# --- Temporary/incognito marker (#246) ------------------------------------


def test_temp_marker_format_and_does_not_leak_username():
    m = marker.temp_marker("mdopp")
    assert re.fullmatch(r"\[temp:[0-9a-f]{8}\] ", m)
    assert "mdopp" not in m
    assert m == f"[temp:{marker.uid_hash('mdopp')}] "


def test_temp_marker_is_hidden_from_household_filter():
    # An ephemeral chat must never appear in the durable household session list.
    temp_title = marker.temp_marker("mdopp") + "secret one-off"
    assert marker.has_marker("mdopp", temp_title) is False
    assert marker.is_temp(temp_title) is True
    # And it is in a distinct namespace from both other marker classes.
    assert marker.is_temp(marker.marker_for("mdopp") + "x") is False
    assert marker.is_temp(marker.maint_marker("mdopp") + "x") is False

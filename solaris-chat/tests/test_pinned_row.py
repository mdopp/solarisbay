"""Frontend-contract checks for the desktop rail nav (#700, was #262).

The household chat is no longer a pinned *session row* in the CHATS list — it is
a primary rail-nav entry (Zuhause) beside Geräte. Its highlight is
derived strictly from the current view (isHouseholdView), so exactly one nav
entry is lit and it never latches. The real check is the box-verify of the
rendered highlight; these assert the markup/wiring that makes it possible.
"""

from __future__ import annotations

import re

from solaris_chat.server import STATIC_DIR

_HTML = (STATIC_DIR / "index.html").read_text(encoding="utf-8")


def _rule_body(selector: str) -> str:
    m = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", _HTML)
    assert m, f"missing CSS rule for {selector}"
    return m.group(1)


def test_household_is_a_rail_nav_entry_not_a_session_row():
    # One Zuhause: a #rail-home nav entry, and no leftover pinned session row.
    assert 'id="rail-home"' in _HTML
    assert 'id="pinned-household"' not in _HTML
    assert 'class="session pinned"' not in _HTML


def test_rail_nav_groups_the_primary_entries():
    m = re.search(r'<nav class="rail-nav">(.*?)</nav>', _HTML, re.S)
    assert m, "missing .rail-nav group"
    group = m.group(1)
    assert 'id="rail-home"' in group
    assert 'id="rail-favorites"' in group
    # #973: energy is no longer a standalone rail entry — it lives in `.energy`.
    assert 'id="rail-energy"' not in group


def test_rail_active_highlight_derives_from_view_state():
    # The nav highlight is toggled from the current view, not a hidden button's
    # class — isHouseholdView is the single source of truth (#700).
    assert "function isHouseholdView()" in _HTML
    assert "sessionId === householdSessionId" in _HTML
    assert 'railHome.classList.toggle("active", onHome)' in _HTML
    # The old latch-prone wiring is gone.
    assert "householdBtn" not in _HTML
    assert "syncPinnedActive" not in _HTML


def test_rail_nav_active_look_is_accent():
    assert "accent-soft" in _rule_body(".rail-nav button.active")

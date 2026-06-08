"""Frontend-contract checks for the pinned household row highlight (#262).

The pinned "Zuhause" row must be visually NEUTRAL by default and only carry the
active highlight when the household chat is the current selection — driven from
selection state, the same mechanism that toggles `.active` on a normal session
row. The real check is the box-verify of the rendered highlight; these assert
the markup/CSS no longer hardcodes the active treatment.
"""

from __future__ import annotations

import re

from solilos_chat.server import STATIC_DIR

_HTML = (STATIC_DIR / "index.html").read_text(encoding="utf-8")


def _rule_body(selector: str) -> str:
    m = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", _HTML)
    assert m, f"missing CSS rule for {selector}"
    return m.group(1)


def test_pinned_rule_is_neutral_by_default():
    body = _rule_body(".session.pinned ")
    assert "accent-soft" not in body
    assert "border-color" not in body


def test_pinned_title_does_not_hardcode_active_look():
    # No `.session.pinned .title` accent/bold rule (was the hardcoded active look).
    assert not re.search(r"\.session\.pinned \.title\s*\{", _HTML)


def test_active_highlight_is_shared_and_toggled_from_selection():
    # The accent highlight lives on the shared `.session.active` rule …
    assert "accent-soft" in _rule_body(".session.active ")
    # … and the pinned row's `.active` is toggled from selection state, like a
    # normal session row — not statically present in its markup.
    assert 'class="session pinned"' in _HTML
    assert 'class="session pinned active"' not in _HTML
    assert 'householdBtn.classList.toggle("active"' in _HTML

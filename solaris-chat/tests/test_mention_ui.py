"""Frontend-contract checks for the #tag / @person mention UI (#279b).

The composer shows an autosuggest popover while typing a `#`/`@` token (reusing
the slash-menu DOM/keyboard idiom and fetching from the unit-279a endpoints),
and sent user turns highlight their mention tokens. The real check is the
box-verify of the rendered popover + chips; these assert the wiring is present.
"""

from __future__ import annotations

import re

from solaris_chat.server import STATIC_DIR

_HTML = (STATIC_DIR / "index.html").read_text(encoding="utf-8")


def test_mention_menu_element_reuses_slash_menu_idiom():
    # A dedicated popover element with the shared .slash-menu class + listbox role.
    m = re.search(r'<div class="slash-menu" id="mention-menu"[^>]*>', _HTML)
    assert m, "missing #mention-menu popover element"
    assert 'role="listbox"' in m.group(0)
    assert "hidden" in m.group(0)


def test_mention_module_fetches_unit_279a_endpoints():
    # `#` autosuggest hits the tags endpoint, `@` hits the persons endpoint.
    assert "/api/mentions/tags?q=" in _HTML
    assert "/api/mentions/persons?q=" in _HTML


def test_mention_token_detection_is_cursor_anchored():
    # tokenAt() scans the text up to the caret for a word-boundary #/@ token.
    assert "function tokenAt()" in _HTML
    assert "input.selectionStart" in _HTML
    assert re.search(r"/\(\[#@\]\)\(\[\\w/-\]\*\)\$/", _HTML), (
        "mention token regex should match a trailing #/@<word> at the caret"
    )


def test_mention_keyboard_nav_wired_into_input_handlers():
    # The mention menu's nav runs in the keydown chain after the slash menu …
    assert re.search(
        r"if \(slash\.handleKey\(e\)\) return;.*\n.*if \(mention\.handleKey\(e\)\) return;",
        _HTML,
    )
    # … and refresh fires on input and on caret-moving keys/clicks.
    assert "slash.refresh(); mention.refresh();" in _HTML
    assert (
        'input.addEventListener("click", function () { mention.refresh(); });' in _HTML
    )
    # Submitting the composer dismisses the popover.
    assert re.search(r"slash\.close\(\);\s*\n\s*mention\.close\(\);", _HTML)


def test_sent_turns_highlight_mentions():
    # User-turn rendering wraps #tag/@person tokens in a styled chip span; both
    # the live-send and history-load paths go through appendMentionText().
    assert "function appendMentionText(el, text)" in _HTML
    assert _HTML.count("appendMentionText(el, text)") >= 2
    # The chip class has a distinct CSS rule (separate tag vs person treatment).
    assert re.search(r"\.mention\s*\{", _HTML)
    assert re.search(r"\.mention\.person\s*\{", _HTML)

"""Frontend-contract checks for the responsive tag-cloud + jump-to-message (#279c).

The active chat's `#tags` / `@persons` render as chips — a right-side gutter
panel on a wide layout, a strip above the composer when narrow — each chip
jumping to (and briefly highlighting) the message it came from. The real check
is the box-verify of the rendered layout + jump; these assert the wiring.
"""

from __future__ import annotations

import re

from solilos_chat.server import STATIC_DIR

_HTML = (STATIC_DIR / "index.html").read_text(encoding="utf-8")


def test_responsive_cloud_containers_present():
    # Two containers — the desktop gutter panel and the narrow strip above the
    # composer — both start hidden and are CSS-toggled by viewport width.
    assert re.search(r'<aside id="tag-cloud"[^>]*\bhidden\b', _HTML)
    assert re.search(r'<div id="tag-cloud-strip"[^>]*\bhidden\b', _HTML)
    # The panel shows only when the viewport clears the centered log; the strip
    # shows below that breakpoint — a responsive either/or.
    assert "@media (min-width: 1240px) { #tag-cloud:not([hidden]) { display: block; }"
    assert re.search(
        r"@media \(min-width: 1240px\) \{ #tag-cloud:not\(\[hidden\]\) \{ display: block;",
        _HTML,
    )
    assert re.search(
        r"@media \(max-width: 1239px\) \{ #tag-cloud-strip:not\(\[hidden\]\) \{ display: flex;",
        _HTML,
    )


def test_cloud_fetches_session_mentions_endpoint():
    # The cloud is loaded from the unit-279c session-mentions endpoint.
    assert 'fetch("/api/sessions/" + encodeURIComponent(sid) + "/mentions")' in _HTML
    assert "function loadTagCloud()" in _HTML


def test_cloud_refreshed_on_open_switch_and_each_turn():
    # Refreshed when a session is opened, on new/incognito chat, and after a turn.
    assert _HTML.count("loadTagCloud();") >= 4
    # The turn-completion path refreshes alongside the session-list reload.
    assert "if (firstTurn) loadSessions(); loadTagCloud();" in _HTML


def test_cloud_chip_jumps_to_message_by_ref():
    # A chip click scrolls to the bubble carrying its message_ref and flashes it.
    assert "function jumpToMention(ref)" in _HTML
    assert "log.querySelector('[data-mention-ref=\"' + ref + '\"]')" in _HTML
    assert 'target.classList.add("search-current")' in _HTML
    assert (
        'chip.addEventListener("click", function () { jumpToMention(it.message_ref); });'
        in _HTML
    )


def test_user_bubbles_carry_mention_ref_in_turn_order():
    # User turns that contain a mention get a sequential data-mention-ref that
    # mirrors the server's per-session message_ref ordinal, so a chip resolves
    # to the right bubble; the counter resets with the transcript.
    assert "function tagUserBubble(el, hadMentions)" in _HTML
    assert 'el.setAttribute("data-mention-ref", String(mentionRefSeq++))' in _HTML
    assert "mentionRefSeq = 0" in _HTML  # reset in clearLog
    # Both user-turn render paths tag the bubble from appendMentionText's count.
    assert _HTML.count("tagUserBubble(el, appendMentionText(el, text) > 0)") >= 2

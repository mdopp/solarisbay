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
    # The mention menu's nav runs in the keydown chain after the slash + dot menus …
    assert re.search(
        r"if \(slash\.handleKey\(e\)\) return;[\s\S]*?"
        r"if \(mention\.handleKey\(e\)\) return;",
        _HTML,
    )
    # … and refresh fires on input and on caret-moving keys/clicks (dot menu rides
    # the same chain, between slash and mention).
    assert "slash.refresh(); dotcmd.refresh(); mention.refresh();" in _HTML
    assert (
        'input.addEventListener("click", function () { mention.refresh(); });' in _HTML
    )
    # Submitting the composer dismisses the popover.
    assert re.search(r"slash\.close\(\);\s*\n\s*mention\.close\(\);", _HTML)


def test_dot_list_rows_open_inline_edit(_html=_HTML):
    # The create·find·EDIT pattern (#967): a tapped task/person row opens a pre-
    # filled inline editor that PATCHes via the *.update actions and re-renders.
    assert "function beginTaskEdit(el, row, t)" in _html
    assert "function beginPersonEdit(el, row, c)" in _html
    # Task row → title area is tappable (not the checkbox) → task.update.
    assert (
        'main.addEventListener("click", function (e) { e.preventDefault(); beginTaskEdit(el, row, t); });'
        in _html
    )
    assert '"task.update"' in _html
    # Person row → tappable → the new person.update action.
    assert (
        'rw.addEventListener("click", function () { beginPersonEdit(el, rw, c); });'
        in _html
    )
    assert '"person.update"' in _html
    # Saving re-renders by reloading the list; the create path is untouched.
    assert re.search(r"beginTaskEdit[\s\S]*?loadTaskList\(el\)", _html)
    assert re.search(r"beginPersonEdit[\s\S]*?loadPersons\(el\)", _html)


def test_photo_dot_command_wired(_html=_HTML):
    # `.photo` (#961) is offered in the dot-command menu with a head label …
    assert re.search(r'\[".photo",', _html)
    assert 'photo: "Foto hochladen"' in _html
    # … and builds a card following buildDocCard's shape: an upload dropzone that
    # POSTs to /api/photo, plus a dc-list filtered by a debounced GET /api/photo?q=.
    assert "function buildPhotoCard(el)" in _html
    assert 'else if (cmd === "photo") buildPhotoCard(card);' in _html
    assert '"/api/photo"' in _html
    assert '"/api/photo?q=" + encodeURIComponent(q)' in _html
    # Typing `.photo <text>` live-filters via searchPhotos in updateCard.
    assert re.search(r'cmd === "photo"[\s\S]*?searchPhotos\(card, vp\)', _html)


def test_home_dot_command_wired(_html=_HTML):
    # `.home` (#980) is offered in the dot-command menu with a head label …
    assert re.search(r'\[".home",', _html)
    assert 'home: "Geräte & Bereiche"' in _html
    # … dispatched to buildHomeCard and updated live in updateCard.
    assert 'else if (cmd === "home") buildHomeCard(card);' in _html
    assert "function buildHomeCard(el)" in _html
    assert re.search(r'cmd === "home"[\s\S]*?renderHomeList\(card, vh\)', _html)
    # It reuses the picker's addable-device endpoint as the full device source
    # and renders matches as controllable widget cards with the ★/☆ favourite
    # toggle (#646, renderHaCard row=false, { pin: true }).
    assert '"/api/portal/start/addable"' in _html
    assert re.search(r"renderHaCard\(c, false, \{ pin: true \}\)", _html)
    # Filtered matches rank the resident's favorites first: a stable partition
    # on pinned_entities, applied BEFORE the render cap so favorites aren't
    # dropped in favour of non-favorites.
    assert "pinned_entities" in _html
    assert re.search(r"favMatches\.concat\(rest\)", _html)
    assert re.search(r"ordered\.slice\(0, 12\)", _html)
    # Energy is its OWN command now (.energy) — .home no longer carries it.
    assert "renderHomeEnergy" not in _html
    # FIND-ONLY: home is never wired into submit()/freeze — no create/submit.
    assert 'kind !== "task" && kind !== "note" && kind !== "contacts"' in _html
    assert '=== "home"' not in re.search(
        r"function submit\(\)[\s\S]*?\n        \}", _html
    ).group(0)


def test_home_cards_register_a_live_host(_html=_HTML):
    # #980: a .home list registers itself as a live host so its widget cards
    # self-update from the SSE/poll stream. Both the filtered-results list and the
    # no-arg favorites list collect {entityId, el} entries and register them.
    assert "function registerHomeLiveHost(listEl, entries)" in _html
    reg = re.search(
        r"function registerHomeLiveHost\(listEl, entries\) \{(.*?)\n        \}",
        _html,
        re.S,
    ).group(1)
    # apply(rec, card) re-renders one widget in place via renderHaCard — with the
    # ★/☆ toggle so a live update doesn't strip it (#646).
    assert "registerLiveHost(listEl, entries" in reg
    assert re.search(r"renderHaCard\(c, false, \{ pin: true \}\)", reg)
    # An empty result unregisters so we don't hold a detached node.
    assert "if (!entries.length) { unregisterLiveHost(listEl); return; }" in reg
    # renderHomeList collects each card's entity_id and registers the list.
    assert re.search(
        r"entries\.push\(\{ entityId: c\.entity_id, c: c, el: cardEl \}\)", _html
    )
    assert "registerHomeLiveHost(listEl, entries);" in _html
    # Tearing down / switching away from the .home dot-card unregisters its host.
    assert "if (card && card._list) unregisterLiveHost(card._list);" in _html


def test_energy_dot_command_wired(_html=_HTML):
    # `.energy` (#980 follow-up) is its own display-only dot-command, split out
    # of `.home` so each command does one thing.
    assert re.search(r'\[".energy",', _html)
    assert 'energy: "Energie & Stromfluss"' in _html
    assert 'else if (cmd === "energy") buildEnergyCard(card);' in _html
    assert "function buildEnergyCard(el)" in _html
    # It reuses the standalone energy renderer inline with a calm fallback.
    assert "renderEnergyPage(listEl, j.energy)" in _html
    assert "Energie ist nicht konfiguriert." in _html
    # Display-only: not wired into submit()/freeze.
    assert '=== "energy"' not in re.search(
        r"function submit\(\)[\s\S]*?\n        \}", _html
    ).group(0)


def test_sent_turns_highlight_mentions():
    # User-turn rendering wraps #tag/@person tokens in a styled chip span; both
    # the live-send and history-load paths go through appendMentionText().
    assert "function appendMentionText(el, text)" in _HTML
    assert _HTML.count("appendMentionText(el, text)") >= 2
    # The chip class has a distinct CSS rule (separate tag vs person treatment).
    assert re.search(r"\.mention\s*\{", _HTML)
    assert re.search(r"\.mention\.person\s*\{", _HTML)

"""Frontend-contract checks for settings-as-/command cards + the /search card.

#410 retires the standalone settings panel: each former Settings tab becomes a
/command that posts its pane as an inline card in the conversation (the model
picker / VRAM bar / skill editor move intact). #411 moves the in-chat search off
the header onto a /search {text} card that searches-as-you-type, and merges the
two stacked mobile bars (topbar + chat-header) into one. The real check is the
box-verify of the rendered cards; these lock the markup/JS contract.
"""

from __future__ import annotations

import re

from solilos_chat.server import STATIC_DIR

_HTML = (STATIC_DIR / "index.html").read_text(encoding="utf-8")


def test_standalone_settings_panel_chrome_is_gone():
    # No settings bar / nav / back button / model nav tab — settings are cards.
    assert 'id="settings-bar"' not in _HTML
    assert 'id="settings-nav"' not in _HTML
    assert 'id="settings-back"' not in _HTML
    assert 'id="nav-model"' not in _HTML
    # showView no longer toggles panel views; it delegates to the card opener.
    assert "function openSettingCard(name)" in _HTML


def test_setting_commands_open_inline_cards():
    # /model, /voice, /skills, /soul, /tools, /thinking route to setting cards
    # (not a panel). /model + /voice stay admin-gated.
    assert (
        'if (cmd === "skills" || cmd === "soul" || cmd === "tools") { openSettingCard(cmd); return; }'
        in _HTML
    )
    assert 'if (cmd === "thinking") { openSettingCard("prefs"); return; }' in _HTML
    assert 'if (cmd === "model" || cmd === "voice") {' in _HTML
    assert 'if (isAdmin) { openSettingCard("model"); }' in _HTML
    # Registered in the slash-menu command list.
    for cmd in (
        "/model",
        "/voice",
        "/skills",
        "/soul",
        "/tools",
        "/thinking",
        "/search",
    ):
        assert '["' + cmd + '"' in _HTML, cmd


def test_model_card_keeps_the_picker_and_vram_loaders():
    # The card hosts the live model pane, so opening it (re)runs the same
    # loaders that drive the picker + VRAM bar from /api/model + /api/vram.
    pane = re.search(r"function loadSettingPane\(name\) \{(.*?)\n      \}", _HTML, re.S)
    assert pane, "loadSettingPane not found"
    body = pane.group(1)
    assert "loadModel(); loadVram(); loadVoice();" in body
    # The model pane (picker + VRAM bar) still exists to be moved into the card.
    assert 'id="view-model"' in _HTML
    assert 'id="model-select"' in _HTML
    assert 'id="vram-bar"' in _HTML


def test_open_setting_card_moves_the_single_live_pane():
    # The one live pane is moved into the newest card; a prior husk is dropped.
    fn = re.search(r"function openSettingCard\(name\) \{(.*?)\n      \}", _HTML, re.S)
    assert fn, "openSettingCard not found"
    body = fn.group(1)
    assert 'var prev = section.closest(".settings-card");' in body
    assert "card.appendChild(section);" in body
    assert "log.appendChild(card);" in body


def test_clear_log_parks_setting_panes_back():
    # Wiping the log must not destroy the moved panes — park them in .main first.
    fn = re.search(r"function clearLog\(\) \{(.*?)\n      \}", _HTML, re.S)
    assert fn, "clearLog not found"
    body = fn.group(1)
    assert "main.appendChild(sec)" in body
    assert "log.contains(sec)" in body


def test_search_command_card_searches_as_you_type():
    # /search posts a card with its own input; input event filters live.
    assert 'if (cmd === "search") { openSearchCard(rest.trim()); return; }' in _HTML
    fn = re.search(r"function openSearchCard\(prefill\) \{(.*?)\n      \}", _HTML, re.S)
    assert fn, "openSearchCard not found"
    body = fn.group(1)
    assert 'searchEl.addEventListener("input", function () { run(false); });' in body
    # Same hit/jump highlight classes as before, and Enter jumps to the next.
    assert 'm.classList.add("search-current");' in body
    assert 'msgs[i].classList.add("search-hit");' in body
    # A prefill (the text after /search) runs immediately and jumps.
    assert "if (prefill) { searchEl.value = prefill; run(true); }" in body


def test_merged_mobile_header_one_bar():
    # The standalone .topbar element is gone; the burger + Solaris wordmark live
    # in the single chat-header now (#411).
    assert '<div class="topbar">' not in _HTML
    header = re.search(r"<header class=\"chat-header\">(.*?)</header>", _HTML, re.S)
    assert header, "chat-header not found"
    hbody = header.group(1)
    assert 'id="rail-toggle"' in hbody  # the burger
    assert 'class="brand-wordmark"' in hbody


def test_wordmark_i_is_the_brand_glyph():
    # The "i" of "Solaris" is replaced by the brand glyph, sized like a letter.
    assert 'Solar<img class="brand-i" src="/static/sol-mark.svg" alt="i" />s' in _HTML
    rule = re.search(r"\.brand-i \{([^}]*)\}", _HTML)
    assert rule, ".brand-i rule missing"
    assert "em" in rule.group(1)  # sized in em so it sits inline like a glyph


def test_burger_and_wordmark_are_mobile_only():
    # On desktop the rail provides this chrome, so both are hidden; the mobile
    # media query reveals them.
    assert ".header-burger { display: none; }" in _HTML
    assert ".brand-wordmark { display: none; }" in _HTML
    mobile = re.search(
        r"@media \(max-width: 760px\) \{\s*/\* One merged header.*?\.header-burger \{ display: inline-flex; \}",
        _HTML,
        re.S,
    )
    assert mobile, "mobile reveal of the merged header missing"

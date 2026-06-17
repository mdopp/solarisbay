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

from solaris_chat.server import STATIC_DIR

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
    # Admin opens the matching card (/model -> model, /voice -> voice); a
    # non-admin gets a clear "admins only" card, not a thin system line.
    assert "openSettingCard(cmd);" in _HTML
    assert "Nur für Admins" in _HTML
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
    # Model card runs the model + VRAM loaders; Voice is its own card now.
    assert "loadModel(); loadVram();" in body
    assert "loadVoice();" in body
    # The model + voice panes still exist to be moved into their cards.
    assert 'id="view-model"' in _HTML
    assert 'id="view-voice"' in _HTML
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
    assert (
        'Solar<img class="brand-i" src="/static/solaris-mark.svg" alt="i" />s' in _HTML
    )
    rule = re.search(r"\.brand-i \{([^}]*)\}", _HTML)
    assert rule, ".brand-i rule missing"
    assert "em" in rule.group(1)  # sized in em so it sits inline like a glyph


def test_help_and_autocomplete_pool_commands_only_not_skills():
    # #482: the `/` autocomplete + /help pool is COMMANDS only — built-in
    # commands PLUS the typeable command-kind templates / inline dual aliases
    # (`commandEntries`). Skills are model-picked and hooks are event-fired, so
    # neither is in the pool: the pool no longer concats `skillEntries`.
    fn = re.search(r"function helpMarkdown\(\) \{(.*?)\n      \}", _HTML, re.S)
    assert fn, "helpMarkdown not found"
    body = fn.group(1)
    assert "commandEntries.length" in body
    assert "commandEntries.forEach" in body
    assert "skillEntries" not in body  # /help no longer lists skills
    # The autocomplete pool concats commandEntries, NOT skillEntries.
    assert "var pool = availableCommands()" in _HTML
    assert ".concat(commandEntries)" in _HTML
    assert ".concat(skillEntries)" not in _HTML
    # skillEntries still exists — it feeds the /skills editor, just not the menu.
    assert "var skillEntries = [];" in _HTML


def test_command_template_runs_as_a_turn():
    # #482: a typeable command `/<id>` expands its body into the turn prompt and
    # runs it (no "Unknown command"); skills/hooks are NOT typeable and fall
    # through. handleCommand routes a `commandDefs` hit to runCommandTemplate.
    fn = re.search(r"function handleCommand\(raw\) \{(.*?)\n      \}", _HTML, re.S)
    assert fn, "handleCommand not found"
    body = fn.group(1)
    assert 'var def = commandDefs["/" + cmd];' in body
    assert "runCommandTemplate(def, rest.trim())" in body
    # The unknown-command fallthrough stays AFTER the command check.
    assert body.index("commandDefs") < body.index('"Unknown command `/"')
    # runCommandTemplate sends the expanded prompt as a real turn.
    tpl = re.search(
        r"function runCommandTemplate\(def, args\) \{(.*?)\n      \}", _HTML, re.S
    )
    assert tpl, "runCommandTemplate not found"
    assert "runTurn(prompt, [])" in tpl.group(1)


def test_commands_card_is_a_setting_card_on_the_defs_api():
    # #482: /commands is a card-command; its editor lists + edits the
    # command-kind registry via /api/defs/command.
    assert '["/commands"' in _HTML
    assert 'if (cmd === "commands") { openSettingCard("commands"); return; }' in _HTML
    assert 'commands: document.getElementById("view-commands")' in _HTML
    assert '/api/defs/command/" + encodeURIComponent(currentCommandId)' in _HTML


def test_skills_card_uses_the_defs_api():
    # #482: the /skills card lists + edits skill-kind defs via /api/defs/skill
    # (with add + delete), not the legacy /api/skills surface.
    assert '/api/defs/skill"' in _HTML  # loadSkills GET list
    assert '/api/defs/skill/" + encodeURIComponent(id)' in _HTML  # openSkill GET
    assert (
        '/api/defs/skill/" + encodeURIComponent(currentSkillId)' in _HTML
    )  # PUT/DELETE


def test_pinned_household_row_opens_the_durable_session():
    # #419: the pinned "Zuhause" row opens the resident's ONE durable household
    # session (from /api/whoami) instead of minting a fresh chat per click; only
    # the very-first-ever turn (no durable row yet, 404) falls back to the
    # pre-bind path that the server routes into the durable id.
    assert "var householdSessionId" in _HTML
    assert (
        "if (j && j.household_session_id) householdSessionId = j.household_session_id;"
        in _HTML
    )
    fn = re.search(r"function startHouseholdChat\(\) \{(.*?)\n      \}", _HTML, re.S)
    assert fn, "startHouseholdChat not found"
    body = fn.group(1)
    assert "if (!householdSessionId) { startHouseholdPrebind(); return; }" in body
    assert "openSession(householdSessionId)" in body
    assert "startHouseholdPrebind();" in body
    # The pre-bind fallback still carries the household topic for the first turn.
    pre = re.search(
        r"function startHouseholdPrebind\(\) \{(.*?)\n      \}", _HTML, re.S
    )
    assert pre and "pendingTopic = HOUSEHOLD_TOPIC;" in pre.group(1)


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

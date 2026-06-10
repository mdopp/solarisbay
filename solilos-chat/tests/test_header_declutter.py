"""Frontend-contract checks for the chat-header persona × speed dropdown (#278).

The separate Thinking (Aus/An) and Persona selectors are combined into ONE
dropdown whose entries pair each persona with a speed (schnell/Thinking),
mapping back to the unchanged payload.personality + payload.reasoning wiring.
It carries the #274 hide paths: hidden in the pinned "Zuhause" chat and in the
ServiceBay-admin embed. The user-facing Thema topic picker is retired (#279d) —
inline #tag/@person mentions replace it; only the internal household binding
stays. The real check is the box-verify across the contexts; these lock the
markup/JS contract.
"""

from __future__ import annotations

import re

from solilos_chat.server import STATIC_DIR

_HTML = (STATIC_DIR / "index.html").read_text(encoding="utf-8")


def test_separate_reasoning_control_is_gone():
    # The standalone Thinking toggle and its select are removed; only the single
    # combined persona dropdown remains in the header.
    assert 'id="reasoning-control"' not in _HTML
    assert 'id="reasoning-mode"' not in _HTML
    assert 'id="persona-control"' in _HTML
    assert 'id="personality"' in _HTML


def test_dropdown_is_fast_only():
    # The household runs one model at one strength: fast. Personas are no longer
    # crossed with a speed — the only speed is fast (reasoning none). Thinking +
    # 12b are reserved for other tasks (the Admin option below).
    assert '[{ suffix: "", reasoning: "none" }]' in _HTML
    assert '{ suffix: "Thinking", reasoning: "high" }' not in _HTML
    assert 'opt.value = p.id + "|" + sp.reasoning;' in _HTML


def test_selection_maps_back_to_persona_and_reasoning():
    # currentPersonality()/currentReasoning() unpack the combined value so the
    # backend personality + reasoning routing is unchanged.
    assert "function parsePersonaSpeed(v)" in _HTML
    assert (
        "function currentPersonality() { return parsePersonaSpeed(personalitySel.value).id; }"
        in _HTML
    )
    assert (
        "function currentReasoning() { return parsePersonaSpeed(personalitySel.value).reasoning; }"
        in _HTML
    )
    # The turn payload still sends both fields.
    assert "personality: currentPersonality(), reasoning: currentReasoning()" in _HTML


def test_household_hides_the_combined_control():
    # syncPinnedActive hides the single combined persona control when the
    # household context is active — the #274 hide path on the new control.
    sync = re.search(
        r"function syncPinnedActive\(activeS\) \{(.*?)\n      \}", _HTML, re.S
    )
    assert sync, "syncPinnedActive not found"
    body = sync.group(1)
    assert "reasoningCtrl" not in body
    assert "personaCtrl.hidden = active" in body
    # Highlight stays selection-driven (no #262 always-highlight regression).
    assert 'householdBtn.classList.toggle("active", active)' in body


def test_thema_picker_is_retired():
    # The user-facing Thema topic picker is gone (#279d): no dropdown element,
    # no fixed-context option gating, no picker-facing JS.
    assert 'id="topic-control"' not in _HTML
    assert 'id="topic-primary"' not in _HTML
    assert 'id="topic-tags"' not in _HTML
    assert "FIXED_CONTEXT_TOPICS" not in _HTML
    assert "function syncSessionTopics" not in _HTML
    assert "function setSessionTopic" not in _HTML
    assert "topicCtrl" not in _HTML


def test_topic_dashboard_modal_is_removed():
    # The #244 topic dashboard modal (only reachable from the removed picker /
    # chip click) is gone; the session-row chip stays as display-only.
    assert 'id="topic-modal"' not in _HTML
    assert "function openTopicDashboard" not in _HTML


def test_embed_hides_persona():
    rule = re.search(r"\.embed #persona-control \{([^}]*)\}", _HTML)
    assert rule, "embed persona hide rule missing"
    assert "display: none" in rule.group(1)
    # The retired Thema control is no longer in the embed hide rule.
    assert "#topic-control" not in _HTML


def test_household_pin_binding_intact():
    # The internal household topic binding stays: the pinned chat pre-binds the
    # `household` topic via the #242 pendingTopic path, and loadTopics surfaces
    # the pin only when the resident can see the household topic.
    assert 'var HOUSEHOLD_TOPIC = "household";' in _HTML
    assert "pendingTopic = HOUSEHOLD_TOPIC;" in _HTML
    assert "payload.topic = pendingTopic;" in _HTML
    assert "householdBtn.hidden = !topicsBySlug[HOUSEHOLD_TOPIC];" in _HTML


def test_chat_search_is_left_aligned():
    # The flex grow spacer now sits AFTER the search wrap, so the in-chat search
    # sits left (next to the title) instead of being pushed right (#280).
    search = _HTML.index('class="chat-search-wrap"')
    grow = _HTML.index('class="chat-head-grow"')
    assert search < grow


def test_household_chat_title_reads_zuhause():
    # In the household context the header title is "Zuhause", not the generic
    # "Neuer Chat" placeholder (#281), without breaking non-household derivation.
    sync = re.search(
        r"function syncPinnedActive\(activeS\) \{(.*?)\n      \}", _HTML, re.S
    )
    assert sync, "syncPinnedActive not found"
    body = sync.group(1)
    assert '"Zuhause"' in body
    assert (
        "topicsBySlug[HOUSEHOLD_TOPIC] && topicsBySlug[HOUSEHOLD_TOPIC].display_name"
        in body
    )
    # The plain renderSessions default for non-household chats is preserved.
    assert (
        'ct.textContent = (activeS && (activeS.title || activeS.preview)) || "Neuer Chat";'
        in _HTML
    )


def test_admin_dropdown_option_selects_admin_gateway():
    # The #293 admin profile is an admin-gated dropdown option whose value packs
    # the maintenance persona id, so a new chat under it routes to the admin
    # Hermes gateway server-side (the server re-checks Remote-Groups).
    assert 'var ADMIN_PERSONA = "servicebay-maintenance";' in _HTML
    assert "function addAdminOption()" in _HTML
    # Gated: only added when the caller is an admin.
    add = re.search(r"function addAdminOption\(\) \{(.*?)\n      \}", _HTML, re.S)
    assert add, "addAdminOption not found"
    body = add.group(1)
    assert "if (!isAdmin) return;" in body
    # The option value carries the maintenance persona (so parsePersonaSpeed +
    # currentPersonality() send it as payload.personality → admin routing).
    assert 'opt.value = ADMIN_PERSONA + "|none";' in body
    assert 'opt.textContent = "Admin";' in body
    # Appended only after BOTH the persona list and whoami (isAdmin) have loaded.
    assert (
        "Promise.all([loadPersonalities(), loadWhoami()]).then(addAdminOption);"
        in _HTML
    )

"""Frontend-contract checks for the chat-header declutter (#274).

Selectors should appear only where there is a real choice: the mode control is
relabelled to a "Thinking" off/on toggle; the pinned "Zuhause" chat hides
Modus/Persona/Thema (fixed values); the Thema picker omits the two system
contexts; and the ServiceBay-admin embed hides Persona + Thema. The real check
is the box-verify across the three contexts; these lock the markup/JS contract.
"""

from __future__ import annotations

import re

from solilos_chat.server import STATIC_DIR

_HTML = (STATIC_DIR / "index.html").read_text(encoding="utf-8")


def test_mode_relabelled_to_thinking_off_on():
    # Label, options, and tooltip are the "Thinking" off/on phrasing now.
    head = _HTML[: _HTML.index('id="topic-control"')]
    assert ">Thinking</label>" in head
    assert ">Aus</option>" in head and ">An</option>" in head
    assert "Schnell" not in head and "Gründlich" not in head
    # Prefs-page copy no longer references the old Schnell/Gründlich wording.
    assert "Gründlich" not in _HTML and "Schnell" not in _HTML
    assert "<strong>Thinking</strong>" in _HTML


def test_household_hides_mode_persona_topic():
    # The reasoning + persona controls carry stable ids the JS toggles, and
    # syncPinnedActive hides all three when the household context is active.
    assert 'id="reasoning-control"' in _HTML
    assert 'id="persona-control"' in _HTML
    sync = re.search(
        r"function syncPinnedActive\(activeS\) \{(.*?)\n      \}", _HTML, re.S
    )
    assert sync, "syncPinnedActive not found"
    body = sync.group(1)
    assert "reasoningCtrl.hidden = active" in body
    assert "personaCtrl.hidden = active" in body
    assert "topicCtrl.hidden = true" in body


def test_thema_picker_omits_system_contexts():
    assert 'FIXED_CONTEXT_TOPICS = { household: 1, "servicebay-admin": 1 }' in _HTML
    # The option-building loop skips the fixed-context slugs.
    assert "if (FIXED_CONTEXT_TOPICS[t.slug]) return;" in _HTML


def test_embed_hides_persona_and_topic():
    rule = re.search(
        r"\.embed #persona-control,\s*\.embed #topic-control \{([^}]*)\}", _HTML
    )
    assert rule, "embed persona/topic hide rule missing"
    assert "display: none" in rule.group(1)

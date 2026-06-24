"""Frontend-contract checks for HA group/list cards (#478, epic #474 phase 4).

When a turn surfaces several entities (a room's lights, an ha_list_entities
set) the cards collapse into one group card — a row per entity, each row
reusing the per-entity control built in phases 2-3. A single entity stays a
standalone card. The real check is the box-verify of the rendered cards; this
locks the markup/JS contract.
"""

from __future__ import annotations

import re

from solaris_chat.server import STATIC_DIR

_HTML = (STATIC_DIR / "index.html").read_text(encoding="utf-8")


def test_multiple_entities_render_as_one_group_card():
    # >1 card -> a single .hc-group container, one row per entity; 1 card stays
    # a standalone card (phases 1-3 unchanged).
    fn = re.search(r"function renderHaCards\(cards\) \{(.*?)\n      \}", _HTML, re.S)
    assert fn, "renderHaCards not found"
    body = fn.group(1)
    assert "cards.length > 1" in body
    assert "hc-group" in body
    # The group iterates entities, rendering each via the shared per-entity card.
    assert "cards.forEach" in body
    assert "renderHaCard(c, true)" in body
    # A single entity still renders one standalone card.
    assert "renderHaCard(cards[0], false)" in body


def test_group_rows_reuse_the_per_entity_controls():
    # renderHaCard holds the phase-2/3 controls and is reused for group rows, so
    # each row carries toggle (light/switch), sliders (brightness/cover), colour,
    # and climate — no duplicated control code for the group case.
    fn = re.search(r"function renderHaCard\(c, row\) \{(.*?)\n      \}", _HTML, re.S)
    assert fn, "renderHaCard not found"
    body = fn.group(1)
    assert 'card.className = row ? "hc-row" : "ha-card";' in body
    assert "haToggle(card, badge, c)" in body
    assert "renderLightControls(card, c)" in body
    assert "renderCoverControls(card, c)" in body
    assert "renderClimateCard(card, c, st)" in body


def test_group_card_has_row_styling():
    # Group rows get their own badge/toggle/busy styling so the controls work
    # inside the group container too.
    assert ".ha-card.hc-group" in _HTML
    assert ".hc-row {" in _HTML
    assert ".hc-row.on .hc-badge" in _HTML
    assert ".hc-row.busy" in _HTML


def test_room_grouping_renders_room_header_or_label():
    # #537: >4 cards group by room (≥2 each) into one hc-group per room with a
    # .hc-room header; the singleton case labels each row via .hc-room-label.
    fn = re.search(r"function renderHaCards\(cards\) \{(.*?)\n      \}\n", _HTML, re.S)
    assert fn, "renderHaCards not found"
    body = fn.group(1)
    assert "roomGroups(cards)" in body
    assert "hc-room" in body  # per-room header
    assert "hc-room-label" in body  # singleton/ungrouped per-card label
    # The grouping rule (every room ≥2) lives in roomGroups, mirroring the engine.
    gf = re.search(r"function roomGroups\(cards\) \{(.*?)\n      \}\n", _HTML, re.S)
    assert gf, "roomGroups not found"
    rb = gf.group(1)
    assert "cards.length <= 4" in rb
    assert ">= 2" in rb
    # Styling for both the header and the inline label exists.
    assert ".hc-room {" in _HTML
    assert ".hc-room-label {" in _HTML

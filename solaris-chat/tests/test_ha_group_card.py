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
    assert "renderLightControls(card, c, badgeHost)" in body
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
    # #540: a single-room set (room query) groups under one header at any count.
    assert "singleRoom" in rb
    # Styling for both the header and the inline label exists.
    assert ".hc-room {" in _HTML
    assert ".hc-room-label {" in _HTML


def test_light_card_is_compact_badge_and_slider_one_row():
    # #538: a light card packs the on/off badge + the brightness slider onto ONE
    # compact row (.hc-compact); the colour picker stays its own row below.
    fn = re.search(r"function renderHaCard\(c, row\) \{(.*?)\n      \}", _HTML, re.S)
    assert fn, "renderHaCard not found"
    body = fn.group(1)
    # The light routes its badge into a .hc-compact host instead of the card.
    assert 'badgeHost.className = "hc-compact"' in body
    assert "badgeHost.appendChild(badge)" in body
    # The brightness slider is mounted on that same compact row.
    assert "renderLightControls(card, c, badgeHost)" in body

    lc = re.search(
        r"function renderLightControls\(card, c, brightHost\) \{(.*?)\n      \}",
        _HTML,
        re.S,
    )
    assert lc, "renderLightControls not found"
    lcb = lc.group(1)
    # Brightness slider goes onto the compact host as an inline control.
    assert "makeSlider(brightHost || card, pct" in lcb
    assert "true);" in lcb  # inline flag

    # makeSlider honours an inline flag (no own row, drag doesn't toggle).
    ms = re.search(
        r"function makeSlider\(card, value, unit, onset, inline\) \{(.*?)\n      \}",
        _HTML,
        re.S,
    )
    assert ms, "makeSlider not found (inline arg)"
    msb = ms.group(1)
    assert 'inline ? "hc-ctrl hc-ctrl-inline" : "hc-ctrl"' in msb
    assert "stopPropagation" in msb

    # Compact-row styling: badge + slider laid out on one flex row.
    assert ".hc-compact { display: flex;" in _HTML
    assert ".hc-compact .hc-ctrl-inline" in _HTML


def test_group_cards_lay_out_side_by_side_on_a_grid():
    # #539: within a group the cards sit in a .hc-grid container so they lay out
    # side-by-side on an invisible grid and wrap to a single column when narrow;
    # the groups themselves keep stacking. Composes with the #537 room grouping.
    fn = re.search(r"function renderHaCards\(cards\) \{(.*?)\n      \}\n", _HTML, re.S)
    assert fn, "renderHaCards not found"
    body = fn.group(1)
    # Both the per-room groups and the single ungrouped group build a grid host.
    assert body.count('grid.className = "hc-grid"') == 2
    assert "grid.appendChild(renderHaCard(c, true))" in body  # room-group rows
    assert "grid.appendChild(row)" in body  # ungrouped rows
    assert "group.appendChild(grid)" in body
    # CSS grid: responsive auto-fill columns with a min width (degrades to 1col).
    assert ".hc-grid {" in _HTML
    assert "display: grid;" in _HTML
    assert "grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));" in _HTML

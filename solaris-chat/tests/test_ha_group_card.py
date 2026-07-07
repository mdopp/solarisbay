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
    assert "renderHaCard(c, true, { pin: true })" in body
    # A single entity still renders one standalone card.
    assert "renderHaCard(cards[0], false, { pin: true })" in body


def test_group_rows_reuse_the_per_entity_controls():
    # renderHaCard holds the phase-2/3 controls and is reused for group rows, so
    # each row carries toggle (light/switch), sliders (brightness/cover), colour,
    # and climate — no duplicated control code for the group case.
    fn = re.search(
        r"function renderHaCard\(c, row, opts\) \{(.*?)\n      \}", _HTML, re.S
    )
    assert fn, "renderHaCard not found"
    body = fn.group(1)
    assert 'card.className = row ? "hc-row" : "ha-card";' in body
    assert "haToggle(card, badge, c)" in body
    assert "renderLightControls(card, c, badgeHost)" in body
    assert "renderCoverControls(card, c, badgeHost)" in body
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
    fn = re.search(
        r"function renderHaCard\(c, row, opts\) \{(.*?)\n      \}", _HTML, re.S
    )
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


def test_all_controllable_cards_use_the_compact_one_row_layout():
    # #550: the #538 compact layout generalizes to every controllable card type
    # — switch/cover/light route the badge into a shared .hc-compact host, and
    # cover/media/climate lay their primary controls on that same compact row.
    fn = re.search(
        r"function renderHaCard\(c, row, opts\) \{(.*?)\n      \}", _HTML, re.S
    )
    assert fn, "renderHaCard not found"
    body = fn.group(1)
    # light/switch/cover all build the shared compact badge host.
    assert (
        'c.domain === "light" || c.domain === "switch" || c.domain === "cover"' in body
    )
    assert 'badgeHost.className = "hc-compact"' in body
    # cover controls now mount onto the compact host.
    assert "renderCoverControls(card, c, badgeHost)" in body

    # makeButtons lays a transport/open-close button row inline on a host.
    mb = re.search(
        r"function makeButtons\(host, specs, onclick, inline\) \{(.*?)\n      \}",
        _HTML,
        re.S,
    )
    assert mb, "makeButtons not found"
    mbb = mb.group(1)
    assert 'inline ? "hc-btns hc-btns-inline" : "hc-btns"' in mbb
    assert "stopPropagation" in mbb

    # cover open/stop/close ride the compact row (inline button row).
    cc = re.search(
        r"function renderCoverControls\(card, c, host\) \{(.*?)\n      \}",
        _HTML,
        re.S,
    )
    assert cc, "renderCoverControls not found"
    ccb = cc.group(1)
    assert "makeButtons(host || card" in ccb
    assert "host != null);" in ccb  # inline when a compact host is given

    # media_player: badge + transport buttons on one compact row.
    mp = re.search(
        r"function renderMediaPlayerCard\(card, c, st\) \{(.*?)\n      \}",
        _HTML,
        re.S,
    )
    assert mp, "renderMediaPlayerCard not found"
    mpb = mp.group(1)
    assert 'compact.className = "hc-compact"' in mpb
    assert "makeButtons(compact," in mpb

    # #551: the now-playing media_player is the wide card — renderHaCard tags it
    # hc-span2 so it spans two base grid columns.
    rc = re.search(
        r"function renderHaCard\(c, row, opts\) \{(.*?)\n      \}", _HTML, re.S
    )
    assert rc, "renderHaCard not found"
    rcb = rc.group(1)
    assert 'card.classList.add("hc-span2")' in rcb

    # climate: current temp + setpoint stepper on one compact row.
    cl = re.search(
        r"function renderClimateCard\(card, c, st\) \{(.*?)\n      \}",
        _HTML,
        re.S,
    )
    assert cl, "renderClimateCard not found"
    clb = cl.group(1)
    assert 'compact.className = "hc-compact"' in clb
    assert 'set.className = "hc-set hc-set-inline"' in clb
    assert "compact.appendChild(set);" in clb

    # Inline button/setpoint rows drop their own margin on the compact row.
    assert ".hc-compact .hc-btns-inline" in _HTML
    assert ".hc-compact .hc-set-inline" in _HTML


def test_group_cards_lay_out_side_by_side_on_a_grid():
    # #551 (supersedes #539): within a group the cards sit in a .hc-grid container
    # on a UNIFORM base-unit grid — every column is the same base width so cards
    # lay out side by side when there's room and collapse to one column when
    # narrow; the groups themselves keep stacking. Composes with #537 grouping.
    fn = re.search(r"function renderHaCards\(cards\) \{(.*?)\n      \}\n", _HTML, re.S)
    assert fn, "renderHaCards not found"
    body = fn.group(1)
    # Both the per-room groups and the single ungrouped group build a grid host.
    assert body.count('grid.className = "hc-grid"') == 2
    assert (
        "grid.appendChild(renderHaCard(c, true, { pin: true }))" in body
    )  # room-group rows
    assert "grid.appendChild(row)" in body  # ungrouped rows
    assert "group.appendChild(grid)" in body
    # CSS grid: a fixed base column width repeated to fill, degrading to 1 column;
    # large cards span a multiple of the same base unit.
    assert ".hc-grid {" in _HTML
    assert "display: grid;" in _HTML
    # #553: the min(100%, --hc-col) guard forces a single full-width column when
    # the container is narrower than the base (phone), so cards never get cramped.
    assert (
        "grid-template-columns: repeat(auto-fill, "
        "minmax(min(100%, var(--hc-col)), 1fr));" in _HTML
    )
    assert "--hc-col:" in _HTML
    # Spanning cards degrade to full-width (span 1) when the grid is 1 column and
    # only widen at a viewport breakpoint, so they never leave half-empty rows.
    assert ".hc-grid .hc-span2 { grid-column: span 2; }" in _HTML


def test_grid_renders_one_full_width_column_on_narrow_no_overflow():
    # #553: on a phone the grid must be ONE full-width column (the min(100%, base)
    # guard), cards fill the column (no 240px cap inside the grid), and control
    # rows / sliders never extend past the card edge.
    # Single-column guard: the track min is min(100%, base), so a viewport
    # narrower than the base collapses to one full-width column.
    assert "minmax(min(100%, var(--hc-col)), 1fr)" in _HTML
    # Cards in the grid drop the 240px cap and can shrink to fit the column.
    assert ".hc-grid > .ha-card { max-width: 100%; min-width: 0; }" in _HTML
    # Cards are border-box so padding/border stay within the column.
    assert "box-sizing: border-box;" in _HTML
    # Sliders take the full row and never claim an intrinsic min that overflows.
    for rule in (
        '.hc-group .hc-ctrl input[type="range"] { flex: 1 1 auto; '
        "min-width: 0; max-width: 100%; }",
        '.ha-card .hc-ctrl input[type="range"] { flex: 1 1 auto; '
        "min-width: 0; max-width: 100%; }",
    ):
        assert rule in _HTML
    # Spanning cards default to full-width (span 1 == "1 / -1") on narrow.
    assert "grid-column: 1 / -1;" in _HTML


def test_answer_container_is_full_width():
    # #551: the Solaris answer bubble spans the full chat column (no 84% cap) so
    # its card grid can fit 2-3 base-width cards side by side.
    m = re.search(r"\.msg\.sol \{([^}]*)\}", _HTML)
    assert m, ".msg.sol rule not found"
    assert "max-width: 100%" in m.group(1)


def test_onoff_control_is_a_toggle_switch_not_a_label_button():
    # #560: the light/switch on/off control reads as a real toggle SWITCH that
    # reflects the live state and flips on click — not an ambiguous "an"/"aus"
    # label-button. The badge gains the .hc-switch class, the card is a role=switch
    # with aria-checked tracking state, and it still routes through haToggle.
    fn = re.search(
        r"function renderHaCard\(c, row, opts\) \{(.*?)\n      \}", _HTML, re.S
    )
    assert fn, "renderHaCard not found"
    body = fn.group(1)
    assert 'badge.classList.add("hc-switch")' in body
    assert 'card.setAttribute("role", "switch")' in body
    assert 'card.setAttribute("aria-checked"' in body
    assert "haToggle(card, badge, c)" in body  # still the existing service-call path

    # haToggle keeps aria-checked in sync on optimistic flip / confirm / revert.
    ht = re.search(
        r"function haToggle\(card, badge, c\) \{(.*?)\n      \}", _HTML, re.S
    )
    assert ht, "haToggle not found"
    assert 'card.setAttribute("aria-checked"' in ht.group(1)

    # The switch is a CSS pill+knob driven by the card's on/off class, with the
    # label text hidden (font-size: 0) so it reads purely as a switch.
    assert ".hc-badge.hc-switch {" in _HTML
    assert "font-size: 0;" in _HTML
    assert ".on .hc-badge.hc-switch::after { transform: translateX(16px); }" in _HTML
    assert ".off .hc-badge.hc-switch {" in _HTML


def test_media_player_card_has_power_toggle_and_source_picker():
    # #561: the media_player card gets a power on/off toggle (gated on
    # TURN_ON/TURN_OFF, reusing the #560 .hc-switch style, calling
    # media_player.turn_on/turn_off) so an off TV is reachable, plus a
    # source/app picker (gated on SELECT_SOURCE) that calls select_source —
    # while keeping the transport + volume controls.
    mp = re.search(
        r"function renderMediaPlayerCard\(card, c, st\) \{(.*?)\n      \}",
        _HTML,
        re.S,
    )
    assert mp, "renderMediaPlayerCard not found"
    mpb = mp.group(1)
    # feature-gated power toggle, same switch style as the light/switch card.
    assert "feat & MP_TURN_ON && feat & MP_TURN_OFF" in mpb
    assert '"hc-badge hc-switch"' in mpb
    assert 'card.classList.contains("on") ? "turn_off" : "turn_on"' in mpb
    # feature-gated source picker calling select_source over the source_list.
    assert "feat & MP_SELECT_SOURCE" in mpb
    assert "c.source_list" in mpb
    assert '"media_player.select_source", { source: sel.value }' in mpb
    # transport + volume still present.
    assert "makeButtons(compact," in mpb
    assert "media_player.volume_set" in mpb
    # the feature bits are defined.
    assert "MP_TURN_ON = 128, MP_TURN_OFF = 256, MP_SELECT_SOURCE = 2048" in _HTML

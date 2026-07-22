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
    # #692: the brightness slider drops into the card BODY (card host); the cover
    # open/stop/close buttons stay on the header badge host.
    assert "renderLightControls(card, c, card)" in body
    assert "renderCoverControls(card, c, badgeHost)" in body
    assert "renderClimateCard(card, c, st, inert)" in body


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


def test_light_card_header_toggle_body_slider():
    # #692: a light card is header [name .... toggle ☆] + a body that is just the
    # brightness slider + %. The on/off badge routes into a .hc-compact host that
    # is appended to the HEADER, and the slider drops into the card body (host =
    # card), so the card is noticeably shorter than the old badge-then-slider row.
    fn = re.search(
        r"function renderHaCard\(c, row, opts\) \{(.*?)\n      \}", _HTML, re.S
    )
    assert fn, "renderHaCard not found"
    body = fn.group(1)
    # The card builds a header row and the name lives in it.
    assert 'head.className = "hc-head"' in body
    assert "head.appendChild(name)" in body
    # The on/off badge routes into a .hc-compact host mounted on the HEADER.
    assert 'badgeHost.className = "hc-compact"' in body
    assert "head.appendChild(badgeHost)" in body
    assert "badgeHost.appendChild(badge)" in body
    # The brightness slider goes into the card BODY (host = card), not the header.
    assert "renderLightControls(card, c, card)" in body
    # The ☆ pin is the last child of the header: [name] .... [toggle] [☆].
    assert "head.appendChild(pin)" in body

    lc = re.search(
        r"function renderLightControls\(card, c, brightHost, stateHost\) \{(.*?)\n      \}",
        _HTML,
        re.S,
    )
    assert lc, "renderLightControls not found"
    lcb = lc.group(1)
    # Brightness slider mounts on the given body host as an inline control.
    assert "makeSlider(brightHost || card, pct" in lcb
    assert ", true, " in lcb  # inline flag
    # #726: tapping the colour swatch must not bubble to the card's power toggle
    # (which would flip the light + re-render the picker away). The colour row
    # stops click + pointerdown propagation — now in the shared hcRenderColourPicker.
    assert (
        'row.addEventListener("click", function (e) { e.stopPropagation(); });' in _HTML
    )
    assert (
        'row.addEventListener("pointerdown", function (e) { e.stopPropagation(); });'
        in _HTML
    )

    # makeSlider honours an inline flag (no own row, drag doesn't toggle).
    ms = re.search(
        r"function makeSlider\(card, value, unit, onset, inline, neutral\) \{(.*?)\n      \}",
        _HTML,
        re.S,
    )
    assert ms, "makeSlider not found (inline arg)"
    msb = ms.group(1)
    assert 'inline ? "hc-ctrl hc-ctrl-inline" : "hc-ctrl"' in msb
    assert "stopPropagation" in msb

    # Header styling: name grows, the toggle/pin sit right-aligned on one row.
    assert ".hc-head {" in _HTML
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
        r"function renderCoverControls\(card, c, host, stateHost\) \{(.*?)\n      \}",
        _HTML,
        re.S,
    )
    assert cc, "renderCoverControls not found"
    ccb = cc.group(1)
    assert "makeButtons(host || card" in ccb
    assert "host != null);" in ccb  # inline when a compact host is given

    # media_player: badge + transport buttons on one compact row.
    mp = re.search(
        r"function renderMediaPlayerCard\(card, c, st, inert\) \{(.*?)\n      \}",
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
        r"function renderClimateCard\(card, c, st, inert\) \{(.*?)\n      \}",
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
    # #692: cards have very different heights, so the container is a packed
    # multi-column (masonry) layout — CSS columns of the base width, as many as
    # the container fits (1 on a phone), tightly packed with break-inside:avoid.
    assert ".hc-grid {" in _HTML
    assert "columns: var(--hc-col);" in _HTML
    assert "--hc-col:" in _HTML
    assert "break-inside: avoid;" in _HTML
    # A wide card (media_player now-playing) spans all columns.
    assert (
        ".hc-grid > .hc-span2,\n    .hc-grid > .hc-span3 { column-span: all; }" in _HTML
    )


def test_grid_renders_one_full_width_column_on_narrow_no_overflow():
    # #692: the masonry uses CSS `columns` of the base width, so a viewport
    # narrower than the base yields ONE full-width column; cards fill it (no 240px
    # cap inside the grid), and control rows / sliders never extend past the edge.
    # A base-width column count collapses to one on a phone.
    assert "columns: var(--hc-col);" in _HTML
    # Cards in the grid drop the 240px cap and can shrink to fit the column.
    assert (
        ".hc-grid > .ha-card, .start-favs > .ha-card"
        " { width: 100%; max-width: 100%; min-width: 0; }" in _HTML
    )
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
    # A card is kept whole in its column (never split across a column break).
    assert "break-inside: avoid;" in _HTML


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


def test_toggle_trusts_optimistic_and_reverts_only_on_hard_failure():
    # #732: on a successful toggle haToggle must NOT apply a server read-back
    # (the server no longer returns one) — it trusts the optimistic target + the
    # SSE/poll card_state. It reverts (apply(was)) only on a hard failure.
    ht = re.search(
        r"function haToggle\(card, badge, c\) \{(.*?)\n      \}", _HTML, re.S
    )
    assert ht, "haToggle not found"
    body = ht.group(1)
    assert "res.state" not in body  # no server read-back applied
    assert "res.ok === false" in body  # hard-failure detection
    assert "apply(was)" in body  # revert path


def test_pending_guard_lives_for_the_full_window():
    # #732: hcPendingContradicts must NOT delete the pending entry on the first
    # matching update — a matching echo returns false but keeps the entry so a
    # later stale echo within the window is still dropped. Only expiry clears it.
    fn = re.search(
        r"function hcPendingContradicts\(entityId, card\) \{(.*?)\n      \}",
        _HTML,
        re.S,
    )
    assert fn, "hcPendingContradicts not found"
    body = fn.group(1)
    # match path returns false WITHOUT deleting:
    assert "if (card && card.state === p.state) return false;" in body
    # exactly one delete (the expiry path), not one on match too:
    assert body.count("delete hcPending[entityId]") == 1

    # The switch is a CSS pill+knob driven by the card's on/off class, with the
    # label text hidden (font-size: 0) so it reads purely as a switch.
    assert ".hc-badge.hc-switch {" in _HTML
    assert "font-size: 0;" in _HTML
    assert ".on .hc-badge.hc-switch::after { transform: translateX(20px); }" in _HTML
    assert ".off .hc-badge.hc-switch {" in _HTML


def test_media_player_card_has_power_toggle_and_source_picker():
    # #561: the media_player card gets a power on/off toggle (gated on
    # TURN_ON/TURN_OFF, reusing the #560 .hc-switch style, calling
    # media_player.turn_on/turn_off) so an off TV is reachable, plus a
    # source/app picker (gated on SELECT_SOURCE) that calls select_source —
    # while keeping the transport + volume controls.
    mp = re.search(
        r"function renderMediaPlayerCard\(card, c, st, inert\) \{(.*?)\n      \}",
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


def test_off_light_shows_last_known_brightness_not_a_fake_100():
    # #733: HA reports brightness:null for an OFF light. The card must cache the
    # last-known % per entity_id and show it when off — never a hard-coded 100%.
    lc = re.search(
        r"function renderLightControls\(card, c, brightHost, stateHost\) \{(.*?)\n      \}",
        _HTML,
        re.S,
    )
    assert lc, "renderLightControls not found"
    lcb = lc.group(1)
    # No fake 100% fallback survives.
    assert ": 100;" not in lcb
    assert "hcLastBrightness[c.entity_id] = Math.round(c.brightness / 255 * 100)" in lcb
    # Off light (brightness null) falls back to the cached %, else neutral 0.
    assert "var cached = hcLastBrightness[c.entity_id];" in lcb
    assert "var pct = known ? cached : (cached != null ? cached : 0);" in lcb
    # A never-seen-on light renders the slider neutral (the 6th makeSlider arg).
    assert "!known && cached == null);" in lcb


def test_widget_climate_card_has_setpoint_stepper_and_mode_selector():
    # #974: the widget renderer (renderHaWidget) must route a climate thermostat
    # to interactive controls — a regression left it falling through to a
    # read-only big-state. Its !inert controls block branches on climate and
    # renderClimateControls builds the −/+ setpoint stepper + HVAC mode picker,
    # routing through haCall like the switch/cover widget controls.
    fn = re.search(r"function renderHaWidget\(c, opts\) \{(.*?)\n      \}", _HTML, re.S)
    assert fn, "renderHaWidget not found"
    body = fn.group(1)
    assert 'domain === "climate"' in body
    # The setpoint stepper mounts on the stateline right; the mode select wraps
    # to a line below (climate may be one row taller than a light — see below).
    assert "renderClimateControls(card, c, st, stateline)" in body

    cc = re.search(
        r"function renderClimateControls\(card, c, st, stateHost\) \{(.*?)\n      \}",
        _HTML,
        re.S,
    )
    assert cc, "renderClimateControls not found"
    ccb = cc.group(1)
    # setpoint stepper gated on CLIMATE_TARGET_TEMP, styled as the widget hc-set,
    # mounted onto the stateline right (setHost = stateHost || card).
    assert "feat & CLIMATE_TARGET_TEMP" in ccb
    assert 'set.className = "hc-set"' in ccb
    assert "var setHost = stateHost || card;" in ccb
    assert "setHost.appendChild(set);" in ccb
    assert 'haCall(card, c, "climate.set_temperature", { temperature: next })' in ccb
    # HVAC mode selector routing through haCall (appended to the card so it wraps
    # below the stateline when it can't fit inline).
    assert 'sel.className = "hc-mode"' in ccb
    assert 'haCall(card, c, "climate.set_hvac_mode", { hvac_mode: sel.value })' in ccb


def test_widget_card_merges_state_and_controls_onto_one_stateline():
    # Compact widget (feat/compact-cards): the standalone card no longer stacks a
    # big header icon + big state + a full-width controls row. It builds a
    # .hc-stateline row that puts the state (small accent icon + value) on the
    # LEFT and the domain controls on the RIGHT, with the thin level bar below.
    fn = re.search(r"function renderHaWidget\(c, opts\) \{(.*?)\n      \}", _HTML, re.S)
    assert fn, "renderHaWidget not found"
    body = fn.group(1)
    # A shared stateline row carries state + controls.
    assert 'stateline.className = "hc-stateline"' in body
    # State (small icon + big value) sits on the LEFT of the stateline.
    assert 'left.className = "hc-state-left"' in body
    assert "left.appendChild(icon)" in body
    assert "left.appendChild(big)" in body
    # The controls render into the SAME stateline row (right side).
    assert "renderCoverControls(card, c, null, stateline)" in body
    assert "renderLightControls(card, c, null, stateline)" in body
    assert "renderSwitchControls(card, c, stateline)" in body
    # The big 38px header icon is gone — no icon is appended to the header row.
    assert "head.appendChild(icon)" not in body
    # Compact padding + the stateline/icon styling exist.
    assert "padding: 12px;" in _HTML
    assert ".ha-card .hc-stateline {" in _HTML
    assert (
        ".ha-card .hc-icon {\n      flex: 0 0 auto; width: 20px; height: 20px;" in _HTML
    )


def test_last_known_brightness_cache_updates_on_every_render():
    # The cache lives alongside the other per-entity client state (hcPending) and
    # is refreshed by renderLightControls, which every render path runs (initial
    # render + SSE card_state + poll all re-render the favorite card).
    assert "var hcLastBrightness = {};" in _HTML
    # A neutral slider shows a dash, not a fabricated %.
    ms = re.search(
        r"function makeSlider\(card, value, unit, onset, inline, neutral\) \{(.*?)\n      \}",
        _HTML,
        re.S,
    )
    assert ms, "makeSlider not found (neutral arg)"
    assert 'val.textContent = neutral ? "–" : value + unit;' in ms.group(1)

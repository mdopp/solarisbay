"""Frontend-contract checks for the per-turn LLM-step trace panel (#307).

Each assistant reply carries a "steps" panel — one row per Ollama call showing
`model · wall_s · tokens`; clicking a step opens a modal with that call's exact
request/response, fetched through the chat server's `/__traces__/<id>`
pass-through (the #305 detail). The panel is loaded from the persisted per-turn
trace (`/api/sessions/<id>/trace`, #306) so it survives chat reload, with the
turns reconstructed by grouping consecutive same-`trace_id` steps and lined up
1:1 with the assistant bubbles in order. The real check is the box-verify of the
rendered panel + click-to-open; these assert the wiring.
"""

from __future__ import annotations

from solilos_chat.server import STATIC_DIR

_HTML = (STATIC_DIR / "index.html").read_text(encoding="utf-8")


def test_panel_loaded_from_persisted_trace_endpoint():
    # Loaded from the #306 persistence endpoint, not just an in-memory live event.
    assert "function loadSessionTrace()" in _HTML
    assert 'fetch("/api/sessions/" + encodeURIComponent(sid) + "/trace")' in _HTML


def test_panel_loaded_on_open_and_after_each_turn():
    # Survives reload (loaded on session open) and updates after a live turn.
    assert _HTML.count("loadSessionTrace();") >= 2


def test_steps_grouped_by_trace_id_into_turns():
    # Consecutive same-trace_id steps form one turn; turns map to the sol bubbles
    # in DOM order, so reopen renders the same per-turn traces in order.
    assert "function renderStepTrace(steps)" in _HTML
    assert "g.trace_id !== s.trace_id" in _HTML
    assert 'log.querySelectorAll(".msg.sol")' in _HTML


def test_step_row_shows_model_time_tokens():
    # Each row: model · wall_s · prompt_tokens.
    assert 'Number(s.wall_s).toFixed(2) + "s"' in _HTML
    assert 's.prompt_tokens + " tok"' in _HTML
    assert "s.model" in _HTML


def test_step_row_shows_profile_badge():
    # Each row carries the Hermes profile that served the call, so household vs
    # admin turns are unambiguous in the trace.
    assert 'prof.className = "st-profile"' in _HTML
    assert "prof.textContent = s.profile" in _HTML


def test_clicking_a_step_opens_the_detail_modal():
    # A step click opens the exact-content modal via its detail_id, fetched from
    # the chat server's /__traces__/<id> pass-through (#305 detail).
    assert "function openTraceDetail(detailId)" in _HTML
    assert 'fetch("/__traces__/" + encodeURIComponent(detailId))' in _HTML
    assert "openTraceDetail(s.detail_id)" in _HTML
    assert '<div class="modal-backdrop" id="trace-modal" hidden>' in _HTML


def test_detail_modal_reads_nested_request_response_shape():
    # /__traces__/<id> returns {path, request:{model,tools,messages},
    # response:{final,tool_calls}} — the modal must read from the nested shape,
    # not flat d.model/d.tools/d.final (#316: flat reads always rendered empty).
    assert "var req = d.request || {};" in _HTML
    assert "var resp = d.response || {};" in _HTML
    assert "var msgs = req.messages || [];" in _HTML
    assert 'section("model", req.model)' in _HTML
    assert 'section("tools[] (" + req.tools.length + ")"' in _HTML
    assert 'section("messages[] (" + msgs.length + ")"' in _HTML
    assert 'section("response", resp.final)' in _HTML
    assert 'section("tool_calls", JSON.stringify(resp.tool_calls, null, 2))' in _HTML
    # No leftover flat-shape reads that the proxy never returns at top level.
    assert "d.model" not in _HTML
    assert "d.final" not in _HTML


def test_panel_not_double_appended_on_refetch():
    # Re-loading the trace (after each turn) must not stack a second panel on a
    # bubble that already has one.
    assert 'meta.querySelector("details.steptrace")' in _HTML

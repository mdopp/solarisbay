"""Quick-reply chips (#555): the offer_choices tool + the SPA contract.

The tool caps/validates options into the turn's choice sink; a turn that calls
it drains a `quick_replies` event (parallel to ha_cards). The SPA renders the
options as chips directly above the composer and sends one on click. The chip
look is operator screenshot-reviewed after deploy; these lock the contract.
"""

from __future__ import annotations

import json
import re
import sqlite3

import pytest

from solaris_chat.engine.ollama import ChatResult
from solaris_chat.engine.tools import Tool, Toolbox
from solaris_chat.engine.tools.choices import build_choice_tools, choice_sink
from solaris_chat.server import STATIC_DIR

from tests.test_engine import _SCHEMA, _client  # shared schema + client harness

_HTML = (STATIC_DIR / "index.html").read_text(encoding="utf-8")


@pytest.fixture
def db(tmp_path) -> str:
    path = str(tmp_path / "solaris.db")
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA)
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def soul(tmp_path) -> str:
    path = tmp_path / "SOUL.md"
    path.write_text("Du bist Solaris.", encoding="utf-8")
    return str(path)


def _offer_tool() -> Tool:
    tools = build_choice_tools()
    assert [t.name for t in tools] == ["offer_choices"]
    return tools[0]


# -- engine: the offer_choices tool ---------------------------------------


def test_offer_choices_description_steers_confirm_first_no_act():
    # #558: the description must make explicit that calling offer_choices means
    # "I am ASKING" — no action tool this turn, wait for the reply — and that
    # it's the right tool for confirming sensitive/irreversible actions.
    desc = _offer_tool().description
    assert "ha_call_service" in desc and "NICHT" in desc
    assert "wartest" in desc
    assert "Garage" in desc or "Schlösser" in desc


@pytest.mark.asyncio
async def test_offer_choices_fills_the_sink():
    sink: list[str] = []
    choice_sink.set(sink)
    out = await _offer_tool().handler({"options": ["ja", "nein"]})
    assert json.loads(out) == {"offered": ["ja", "nein"]}
    assert sink == ["ja", "nein"]


@pytest.mark.asyncio
async def test_offer_choices_caps_at_four_and_dedupes():
    sink: list[str] = []
    choice_sink.set(sink)
    await _offer_tool().handler({"options": ["a", "b", "a", " c ", "d", "e", ""]})
    # blanks dropped, duplicate "a" dropped, trimmed, capped at 4
    assert sink == ["a", "b", "c", "d"]


@pytest.mark.asyncio
async def test_offer_choices_rejects_empty():
    sink: list[str] = []
    choice_sink.set(sink)
    out = await _offer_tool().handler({"options": []})
    assert "error" in json.loads(out)
    assert sink == []


@pytest.mark.asyncio
async def test_turn_emits_quick_replies_event(db, soul):
    # A turn where the model calls offer_choices drains the sink into a single
    # `quick_replies` event, emitted once near run.completed (like ha_cards).
    results = [
        ChatResult(
            tool_calls=[
                {
                    "function": {
                        "name": "offer_choices",
                        "arguments": {"options": ["ja", "nein"]},
                    }
                }
            ],
            prompt_tokens=40,
            completion_tokens=5,
        ),
        ChatResult(content="Soll ich die Garage öffnen?", prompt_tokens=50),
    ]
    client, _ = _client(db, soul, results, tools=build_choice_tools())
    sid = await client.create_session("anna")
    events = [e async for e in client.chat_stream(sid, "Garage")]
    qr = [e for e in events if e["type"] == "quick_replies"]
    assert len(qr) == 1
    assert qr[0]["data"]["options"] == ["ja", "nein"]
    kinds = [e["type"] for e in events]
    assert kinds.index("quick_replies") < kinds.index("run.completed")


@pytest.mark.asyncio
async def test_turn_without_offer_emits_no_quick_replies(db, soul):
    client, _ = _client(
        db, soul, [ChatResult(content="Hallo")], tools=build_choice_tools()
    )
    sid = await client.create_session("anna")
    events = [e async for e in client.chat_stream(sid, "Hi")]
    assert not any(e["type"] == "quick_replies" for e in events)


def test_dispatch_unknown_args_is_safe():
    # The Toolbox swallows a bad-shaped call into a model-facing error, never a
    # turn-killer — the sink stays empty.
    box = Toolbox(build_choice_tools())

    async def run():
        choice_sink.set([])
        return await box.dispatch("offer_choices", {"options": "not-a-list"})

    import asyncio

    out = asyncio.run(run())
    assert "error" in json.loads(out)


# -- SPA: chips above the composer + click-to-send ------------------------


def test_quick_reply_row_sits_above_the_composer():
    # The chip row is a child of .composer-bar, BEFORE the <form id="composer">,
    # so it renders directly above the input field.
    bar = re.search(r'<div class="composer-bar">(.*?)</div>\s*</section>', _HTML, re.S)
    assert bar, "composer-bar not found"
    body = bar.group(1)
    assert 'id="quick-replies"' in body
    assert body.index('id="quick-replies"') < body.index('<form id="composer">')
    # styled as a compact chip row.
    assert ".quick-replies {" in _HTML
    assert ".quick-reply-chip {" in _HTML


def test_quick_replies_event_renders_chips():
    # The stream handler routes the `quick_replies` event to renderQuickReplies.
    assert (
        'else if (event === "quick_replies") { renderQuickReplies(d.options); }'
        in _HTML
    )
    fn = re.search(
        r"function renderQuickReplies\(options\) \{(.*?)\n      \}", _HTML, re.S
    )
    assert fn, "renderQuickReplies not found"
    body = fn.group(1)
    assert "options.slice(0, 4)" in body  # cap mirrored client-side
    assert 'b.className = "quick-reply-chip";' in body


def test_chip_click_sends_immediately_and_clears():
    fn = re.search(
        r"function renderQuickReplies\(options\) \{(.*?)\n      \}", _HTML, re.S
    )
    body = fn.group(1)
    # click = clear the row, then push the text as the next user turn + run it.
    assert "clearQuickReplies();" in body
    assert "addUserTurn(text, []);" in body
    assert "runTurn(text, []);" in body


def test_chips_clear_on_any_other_send():
    # A fresh composer submit retires the previous turn's chips.
    submit = re.search(
        r'form\.addEventListener\("submit", function \(e\) \{(.*?)\n      \}\);',
        _HTML,
        re.S,
    )
    assert submit and "clearQuickReplies();" in submit.group(1)
    # Switching/clearing a session also clears them.
    cl = re.search(r"function clearLog\(\) \{(.*?)\n      \}", _HTML, re.S)
    assert cl and "clearQuickReplies();" in cl.group(1)

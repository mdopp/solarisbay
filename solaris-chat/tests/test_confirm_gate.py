"""Deterministic confirmation gate for sensitive HA actions (#570).

The prompt-only confirm-before-act (u64/#558) is unreliable on gemma4:e4b —
"Garagentor öffnen" sometimes executes with a bare "Klar.". This locks the
code-enforced gate: a sensitive ha_call_service is NOT executed on the turn the
model issues it (no matter what the model does); it is held, ja/nein chips are
offered, and only an affirmative follow-up actually invokes HA.

HA is stubbed (read-only, per the house rule) and the POST body recorded, so a
held call asserts NO service was invoked; an executed one asserts exactly one.
"""

from __future__ import annotations

import sqlite3

import pytest

from solaris_chat.engine import areas as areas_mod
from solaris_chat.engine import confirm
from solaris_chat.engine.ollama import ChatResult
from solaris_chat.engine.tools import ha as ha_mod
from solaris_chat.engine.tools.choices import build_choice_tools
from solaris_chat.engine.tools.ha import build_ha_tools

from tests.test_engine import _SCHEMA, _client  # shared schema + client harness


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


def _stub_ha(monkeypatch) -> list[tuple[str, dict]]:
    """Stub aiohttp so a ha_call_service POST is recorded, not sent. Returns the
    list of (url, body) POSTs the run actually issued."""
    posts: list[tuple[str, dict]] = []

    class _Resp:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def json(self):
            return {"ok": True}

        async def text(self):
            return ""

    class _Session:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def post(self, url, *, json, **k):
            posts.append((url, json))
            return _Resp()

        def get(self, url, **k):
            return _Resp()

        def ws_connect(self, _url):
            raise OSError("no ws")

    monkeypatch.setattr(ha_mod.aiohttp, "ClientSession", _Session)
    monkeypatch.setattr(areas_mod.aiohttp, "ClientSession", _Session)
    return posts


def _tools():
    return build_ha_tools("http://ha", "tok") + build_choice_tools()


def _open_garage_call() -> ChatResult:
    return ChatResult(
        tool_calls=[
            {
                "function": {
                    "name": "ha_call_service",
                    "arguments": {
                        "domain": "cover",
                        "service": "open_cover",
                        "entity_id": "cover.garage_door",
                    },
                }
            }
        ],
        prompt_tokens=40,
    )


# -- policy units ---------------------------------------------------------


def test_classify_sensitive_by_service_and_domain():
    assert confirm.is_sensitive("cover", "open_cover")
    assert confirm.is_sensitive("lock", "unlock")
    assert confirm.is_sensitive("alarm_control_panel", "alarm_disarm")
    assert confirm.is_sensitive("lock", "lock")  # whole lock domain is gated
    # routine controls are not sensitive
    assert not confirm.is_sensitive("light", "turn_on")
    assert not confirm.is_sensitive("cover", "close_cover")
    assert not confirm.is_sensitive("switch", "turn_off")


def test_affirmative_negative_detection():
    assert confirm.is_affirmative("ja")
    assert confirm.is_affirmative("Ja bitte öffnen")
    assert confirm.is_affirmative("mach")
    assert not confirm.is_affirmative("nein")
    assert confirm.is_negative("nein")
    assert confirm.is_negative("Stop, abbrechen")
    # negative wins on a mixed reply — never auto-execute on ambiguity
    assert not confirm.is_affirmative("nein doch nicht")


# -- the gate, end to end -------------------------------------------------


@pytest.mark.asyncio
async def test_sensitive_call_held_not_executed_on_turn1(db, soul, monkeypatch):
    posts = _stub_ha(monkeypatch)
    results = [
        _open_garage_call(),
        ChatResult(
            content="Soll ich das Garagentor wirklich öffnen?", prompt_tokens=50
        ),
    ]
    client, _ = _client(db, soul, results, tools=_tools())
    sid = await client.create_session("anna")
    events = [e async for e in client.chat_stream(sid, "Garagentor öffnen")]

    # The underlying HA service was NOT invoked.
    assert posts == []
    # ja/nein chips were offered so the mic stays open / chips show.
    qr = [e for e in events if e["type"] == "quick_replies"]
    assert qr and qr[0]["data"]["options"] == ["ja", "nein"]
    # The pending action is stashed for the next turn.
    pending = client._pending.peek(sid)
    assert pending is not None
    assert (pending.domain, pending.service, pending.entity_id) == (
        "cover",
        "open_cover",
        "cover.garage_door",
    )


@pytest.mark.asyncio
async def test_affirmative_followup_executes_stashed_action(db, soul, monkeypatch):
    posts = _stub_ha(monkeypatch)
    client, _ = _client(
        db,
        soul,
        [
            _open_garage_call(),
            ChatResult(content="Soll ich das Garagentor wirklich öffnen?"),
            # turn 2: the model's natural report after the confirmed tool result
            ChatResult(content="Erledigt, das Garagentor ist offen."),
        ],
        tools=_tools(),
    )
    sid = await client.create_session("anna")
    _ = [e async for e in client.chat_stream(sid, "Garagentor öffnen")]
    assert posts == []  # still not executed

    _ = [e async for e in client.chat_stream(sid, "ja")]
    # exactly one HA service call, the stashed open_cover
    assert len(posts) == 1
    url, body = posts[0]
    assert url.endswith("/api/services/cover/open_cover")
    assert body["entity_id"] == "cover.garage_door"
    # the pending slot is cleared
    assert client._pending.peek(sid) is None


@pytest.mark.asyncio
async def test_negative_followup_drops_stashed_action(db, soul, monkeypatch):
    posts = _stub_ha(monkeypatch)
    client, _ = _client(
        db,
        soul,
        [
            _open_garage_call(),
            ChatResult(content="Soll ich das Garagentor wirklich öffnen?"),
            ChatResult(content="Ok, ich lasse es zu."),
        ],
        tools=_tools(),
    )
    sid = await client.create_session("anna")
    _ = [e async for e in client.chat_stream(sid, "Garagentor öffnen")]
    _ = [e async for e in client.chat_stream(sid, "nein")]
    # nothing was ever executed and the pending slot is cleared
    assert posts == []
    assert client._pending.peek(sid) is None


@pytest.mark.asyncio
async def test_routine_action_executes_directly(db, soul, monkeypatch):
    posts = _stub_ha(monkeypatch)
    client, _ = _client(
        db,
        soul,
        [
            ChatResult(
                tool_calls=[
                    {
                        "function": {
                            "name": "ha_call_service",
                            "arguments": {
                                "domain": "light",
                                "service": "turn_on",
                                "entity_id": "light.kitchen",
                            },
                        }
                    }
                ]
            ),
            ChatResult(content="Licht ist an."),
        ],
        tools=_tools(),
    )
    sid = await client.create_session("anna")
    events = [e async for e in client.chat_stream(sid, "Licht an")]
    # executed directly, no confirmation held, no chips
    assert len(posts) == 1
    assert posts[0][0].endswith("/api/services/light/turn_on")
    assert client._pending.peek(sid) is None
    assert not any(e["type"] == "quick_replies" for e in events)


@pytest.mark.asyncio
async def test_gate_normalizes_cover_open_alias(db, soul, monkeypatch):
    # The model often issues the bare verb "open" for a cover; call_service
    # aliases it to open_cover, so the gate must classify it sensitive too.
    posts = _stub_ha(monkeypatch)
    client, _ = _client(
        db,
        soul,
        [
            ChatResult(
                tool_calls=[
                    {
                        "function": {
                            "name": "ha_call_service",
                            "arguments": {
                                "domain": "cover",
                                "service": "open",
                                "entity_id": "cover.garage_door",
                            },
                        }
                    }
                ]
            ),
            ChatResult(content="Soll ich das Garagentor wirklich öffnen?"),
        ],
        tools=_tools(),
    )
    sid = await client.create_session("anna")
    _ = [e async for e in client.chat_stream(sid, "Garage auf")]
    assert posts == []
    pending = client._pending.peek(sid)
    assert pending is not None and pending.service == "open_cover"

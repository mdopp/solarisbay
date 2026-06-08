"""Tests for the session-based Hermes client (#142).

The client talks to Hermes' native API: create a session via
`POST /api/sessions`, then send turns to `POST /api/sessions/{id}/chat`
with `{"input": ...}`, reading the reply from `{"message": {"content": ...}}`.
HTTP is mocked with httpx.MockTransport so the contract is exercised without
a live Hermes.
"""

from __future__ import annotations

import httpx
import pytest

from gatekeeper import marker
from gatekeeper.hermes import HermesClient, _extract_reply, _is_low_quality_reply


def _install_transport(monkeypatch, handler):
    """Route the client's httpx.AsyncClient through a mock transport."""
    real_init = httpx.AsyncClient.__init__

    def patched_init(self, *args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched_init)


async def test_create_session_then_chat_round_trip(monkeypatch):
    calls: list[tuple[str, str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        body = json.loads(request.content) if request.content else {}
        calls.append((request.method, request.url.path, body))
        if request.url.path == "/api/sessions":
            return httpx.Response(200, json={"id": "sess-1"})
        if request.url.path == "/api/sessions/sess-1/chat":
            return httpx.Response(200, json={"message": {"content": "pongII"}})
        return httpx.Response(404)

    _install_transport(monkeypatch, handler)
    client = HermesClient("http://hermes:8642", "secret")
    reply = await client.converse(
        text="ping", uid="michael", endpoint="voice-pe:sat", trace_id="t1"
    )

    assert reply == "pongII"
    assert calls[0][:2] == ("POST", "/api/sessions")
    # The create body carries the resident's uid marker as the seed title (#153).
    assert calls[0][2] == {
        "user_id": "michael",
        "title": marker.marker_for("michael"),
    }
    assert calls[1][:2] == ("POST", "/api/sessions/sess-1/chat")
    # Default voice turn is FAST: reasoning_effort "none", no thinking surfaced.
    assert calls[1][2] == {"input": "ping", "reasoning_effort": "none"}


async def test_bearer_token_sent(monkeypatch):
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("Authorization"))
        if request.url.path == "/api/sessions":
            return httpx.Response(200, json={"id": "s"})
        return httpx.Response(200, json={"message": {"content": "ok"}})

    _install_transport(monkeypatch, handler)
    client = HermesClient("http://hermes:8642", "secret")
    await client.converse(text="hi", uid="u", endpoint="e", trace_id="t")

    assert seen == ["Bearer secret", "Bearer secret"]


async def test_session_reused_across_turns(monkeypatch):
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/api/sessions":
            return httpx.Response(200, json={"id": "sess-A"})
        return httpx.Response(200, json={"message": {"content": "reply"}})

    _install_transport(monkeypatch, handler)
    client = HermesClient("http://hermes:8642", "")

    await client.converse(text="first", uid="michael", endpoint="e", trace_id="t1")
    await client.converse(text="second", uid="michael", endpoint="e", trace_id="t2")

    # Exactly one session created; the second turn reuses the cached id.
    assert paths.count("/api/sessions") == 1
    assert paths.count("/api/sessions/sess-A/chat") == 2


async def test_expired_session_recreated_once(monkeypatch):
    state = {"session_calls": 0}
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/api/sessions":
            state["session_calls"] += 1
            return httpx.Response(200, json={"id": "new"})
        if request.url.path == "/api/sessions/old/chat":
            return httpx.Response(404, text="session expired")
        if request.url.path == "/api/sessions/new/chat":
            return httpx.Response(200, json={"message": {"content": "recovered"}})
        return httpx.Response(500)

    _install_transport(monkeypatch, handler)
    client = HermesClient("http://hermes:8642", "")
    # Seed a stale session id so the first chat hits the 404 path.
    client._sessions["michael"] = "old"

    reply = await client.converse(text="hi", uid="michael", endpoint="e", trace_id="t")

    assert reply == "recovered"
    assert state["session_calls"] == 1  # recreated exactly once
    assert client._sessions["michael"] == "new"


async def test_chat_error_returns_empty(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/sessions":
            return httpx.Response(200, json={"id": "s"})
        return httpx.Response(500, text="boom")

    _install_transport(monkeypatch, handler)
    client = HermesClient("http://hermes:8642", "")
    reply = await client.converse(text="hi", uid="u", endpoint="e", trace_id="t")
    assert reply == ""


@pytest.mark.parametrize(
    "reply,low",
    [
        ("", True),
        ("   ", True),
        ("hm", True),  # short, not a confirmation -> miss
        ("idk", True),
        ("ok", False),  # short confirmation -> good
        ("Alles klar.", False),
        ("Done!", False),
        ("The living room light is now on.", False),  # long -> good
    ],
)
def test_is_low_quality_reply(reply, low):
    assert _is_low_quality_reply(reply) is low


def _routing_handler(record, *, fast_reply, slow_reply="from slow"):
    """Mock transport: fast session id 'sess-fast', slow id 'sess-slow'.

    The model field in the create body selects which id is returned, so the
    test can assert which session each chat turn hit.
    """
    import json

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        record.append((request.url.path, body))
        if request.url.path == "/api/sessions":
            sid = "sess-fast" if body.get("model") else "sess-slow"
            return httpx.Response(200, json={"id": sid})
        if request.url.path == "/api/sessions/sess-fast/chat":
            return httpx.Response(200, json={"message": {"content": fast_reply}})
        if request.url.path == "/api/sessions/sess-slow/chat":
            return httpx.Response(200, json={"message": {"content": slow_reply}})
        return httpx.Response(404)

    return handler


async def test_fast_success_no_fallback(monkeypatch):
    record: list[tuple[str, dict]] = []
    _install_transport(
        monkeypatch,
        _routing_handler(record, fast_reply="The living room light is now on."),
    )
    client = HermesClient("http://hermes:8642", "", fast_model="gemma4:e2b")

    reply = await client.converse(
        text="lights on", uid="michael", endpoint="e", trace_id="t"
    )

    assert reply == "The living room light is now on."
    paths = [p for p, _ in record]
    # Only the fast session is created + used; the slow session never runs.
    assert "/api/sessions/sess-slow/chat" not in paths
    assert record[0][1] == {
        "user_id": "michael",
        "title": marker.marker_for("michael"),
        "model": "gemma4:e2b",
    }


async def test_fast_empty_falls_back_to_slow(monkeypatch):
    record: list[tuple[str, dict]] = []
    _install_transport(
        monkeypatch,
        _routing_handler(record, fast_reply="", slow_reply="recovered by slow"),
    )
    client = HermesClient("http://hermes:8642", "", fast_model="gemma4:e2b")

    reply = await client.converse(
        text="complex", uid="michael", endpoint="e", trace_id="t"
    )

    assert reply == "recovered by slow"
    paths = [p for p, _ in record]
    assert "/api/sessions/sess-fast/chat" in paths
    assert "/api/sessions/sess-slow/chat" in paths
    # The slow session is created without a model override (uses Hermes default).
    slow_create = next(
        b for p, b in record if p == "/api/sessions" and not b.get("model")
    )
    assert slow_create == {"user_id": "michael", "title": marker.marker_for("michael")}


async def test_fast_too_short_falls_back_to_slow(monkeypatch):
    record: list[tuple[str, dict]] = []
    _install_transport(
        monkeypatch,
        _routing_handler(record, fast_reply="hm?", slow_reply="full slow answer"),
    )
    client = HermesClient("http://hermes:8642", "", fast_model="gemma4:e2b")

    reply = await client.converse(text="q", uid="michael", endpoint="e", trace_id="t")

    assert reply == "full slow answer"
    assert "/api/sessions/sess-slow/chat" in [p for p, _ in record]


async def test_thorough_cue_skips_fast_model(monkeypatch):
    # A THOROUGH turn (explicit "think harder" cue) must bypass the fast model
    # entirely and go straight to the slow (12b default) session — model routing
    # follows the reasoning effort, not just the reply-quality fallback.
    record: list[tuple[str, dict]] = []
    _install_transport(
        monkeypatch,
        _routing_handler(record, fast_reply="should never run", slow_reply="deep"),
    )
    client = HermesClient("http://hermes:8642", "", fast_model="gemma4:e2b")

    reply = await client.converse(
        text="denk mal scharf nach und erklär mir das genau",
        uid="michael",
        endpoint="e",
        trace_id="t",
    )

    assert reply == "deep"
    paths = [p for p, _ in record]
    # The fast session is never created or used.
    assert "/api/sessions/sess-fast/chat" not in paths
    assert all(not b.get("model") for p, b in record if p == "/api/sessions")
    # The slow turn carries the escalated reasoning_effort.
    slow_chat = [b for p, b in record if p == "/api/sessions/sess-slow/chat"]
    assert slow_chat and slow_chat[0]["reasoning_effort"] == "high"


async def test_no_fast_model_single_session_passthrough(monkeypatch):
    record: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        body = json.loads(request.content) if request.content else {}
        record.append((request.url.path, body))
        if request.url.path == "/api/sessions":
            return httpx.Response(200, json={"id": "only"})
        return httpx.Response(200, json={"message": {"content": "x"}})

    _install_transport(monkeypatch, handler)
    client = HermesClient("http://hermes:8642", "")  # fast_model unset

    await client.converse(text="hi", uid="michael", endpoint="e", trace_id="t")

    # Exactly one session, no model override in the create body.
    creates = [b for p, b in record if p == "/api/sessions"]
    assert creates == [{"user_id": "michael", "title": marker.marker_for("michael")}]
    assert client._sessions == {"michael": "only"}


async def test_default_turn_sends_fast_reasoning(monkeypatch):
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        if request.url.path == "/api/sessions":
            return httpx.Response(200, json={"id": "s"})
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"message": {"content": "ok"}})

    _install_transport(monkeypatch, handler)
    client = HermesClient("http://hermes:8642", "")
    await client.converse(
        text="welche Lichter sind an", uid="u", endpoint="e", trace_id="t"
    )
    assert bodies == [{"input": "welche Lichter sind an", "reasoning_effort": "none"}]


async def test_explicit_cue_escalates_reasoning(monkeypatch):
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        if request.url.path == "/api/sessions":
            return httpx.Response(200, json={"id": "s"})
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"message": {"content": "ok"}})

    _install_transport(monkeypatch, handler)
    client = HermesClient("http://hermes:8642", "")
    await client.converse(
        text="Denk mal scharf nach: warum tropft der Wasserhahn",
        uid="u",
        endpoint="e",
        trace_id="t",
    )
    # Cue escalates to thorough; voice never surfaces the reasoning block.
    assert bodies == [
        {
            "input": "Denk mal scharf nach: warum tropft der Wasserhahn",
            "reasoning_effort": "high",
        }
    ]


@pytest.mark.parametrize(
    "body,expected",
    [
        ({"message": {"content": "pongII"}}, "pongII"),
        ({"output": "out"}, "out"),
        ({"reply": "rep"}, "rep"),
        ({"response": "resp"}, "resp"),
        ({"text": "txt"}, "txt"),
        ({"message": {}}, ""),
        ({}, ""),
        ("not-a-dict", ""),
    ],
)
def test_extract_reply_shapes(body, expected):
    assert _extract_reply(body) == expected

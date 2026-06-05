"""Tests for uid mapping and the create-then-chat session flow."""

from __future__ import annotations

from oscar_chat.hermes import (
    _extract_messages,
    _extract_reply,
    _extract_session_id,
    _iter_session_items,
    _maybe_json,
    _session_owner,
    _session_summary,
)
from oscar_chat.server import _normalize, build_app, resolve_uid


class _FakeRequest:
    def __init__(self, headers: dict[str, str]):
        self.headers = headers


def test_resolve_uid_from_header():
    req = _FakeRequest({"Remote-User": "mdopp"})
    assert resolve_uid(req, "Remote-User", "household") == "mdopp"


def test_resolve_uid_strips_whitespace():
    req = _FakeRequest({"Remote-User": "  mdopp  "})
    assert resolve_uid(req, "Remote-User", "household") == "mdopp"


def test_resolve_uid_falls_back_when_header_absent():
    req = _FakeRequest({})
    assert resolve_uid(req, "Remote-User", "household") == "household"


def test_resolve_uid_falls_back_on_empty_header():
    req = _FakeRequest({"Remote-User": "   "})
    assert resolve_uid(req, "Remote-User", "household") == "household"


def test_extract_session_id_shapes():
    assert _extract_session_id({"id": "abc"}) == "abc"
    assert _extract_session_id({"session_id": "def"}) == "def"
    assert _extract_session_id({"session": {"id": "ghi"}}) == "ghi"
    assert _extract_session_id({}) == ""
    assert _extract_session_id(None) == ""


def test_extract_reply_shapes():
    assert _extract_reply({"output": "hi"}) == "hi"
    assert _extract_reply({"reply": "yo"}) == "yo"
    assert _extract_reply({"response": "ok"}) == "ok"
    assert (
        _extract_reply({"message": {"role": "assistant", "content": "hello"}})
        == "hello"
    )
    assert _extract_reply({}) == ""


class _FakeHermes:
    def __init__(self, events=None, store=None):
        self.created = []
        self.turns = []
        self._events = events or []
        # store: list of {id, user_id, title, last_activity, messages}
        self._store = store or []

    async def create_session(self, uid):
        self.created.append(uid)
        return "sess-1"

    async def chat(self, session_id, text):
        self.turns.append((session_id, text))
        return f"echo: {text}"

    async def chat_stream(self, session_id, text):
        self.turns.append((session_id, text))
        for event in self._events:
            yield event

    async def list_sessions(self, uid):
        # Mirror the real client's scoping invariant: only this uid's sessions.
        return [
            {
                "id": s["id"],
                "title": s.get("title", ""),
                "last_activity": s.get("last_activity", ""),
            }
            for s in self._store
            if s.get("user_id") == uid
        ]

    async def get_session(self, session_id, uid):
        for s in self._store:
            if s["id"] == session_id and s.get("user_id") == uid:
                return {
                    "id": s["id"],
                    "title": s.get("title", ""),
                    "last_activity": s.get("last_activity", ""),
                    "messages": s.get("messages", []),
                }
        return None


def test_iter_session_items_envelopes():
    assert _iter_session_items([{"id": "a"}]) == [{"id": "a"}]
    assert _iter_session_items({"sessions": [{"id": "b"}]}) == [{"id": "b"}]
    assert _iter_session_items({"items": [{"id": "c"}]}) == [{"id": "c"}]
    assert _iter_session_items({"nope": 1}) == []


def test_session_owner_and_summary():
    assert _session_owner({"user_id": "mdopp"}) == "mdopp"
    assert _session_owner({"owner": "lena"}) == "lena"
    assert _session_owner({}) == ""
    summ = _session_summary(
        {"id": "x", "title": "Trip", "last_activity": "2026-06-05T10:00:00Z"}
    )
    assert summ == {
        "id": "x",
        "title": "Trip",
        "last_activity": "2026-06-05T10:00:00Z",
    }


def test_extract_messages_shapes():
    raw = {
        "messages": [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": [{"text": "he"}, {"text": "llo"}]},
            {"role": "system", "content": ""},
        ]
    }
    assert _extract_messages(raw) == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]
    assert _extract_messages({}) == []


def test_normalize_assistant_delta():
    assert _normalize({"type": "assistant.delta", "data": {"delta": "hi"}}) == (
        "delta",
        {"text": "hi"},
    )


def test_normalize_delta_text_and_string_payload():
    assert _normalize({"type": "assistant.delta", "data": {"text": "yo"}}) == (
        "delta",
        {"text": "yo"},
    )
    assert _normalize({"type": "assistant.delta", "data": "raw"}) == (
        "delta",
        {"text": "raw"},
    )


def test_normalize_tool_events():
    assert _normalize({"type": "tool.started", "data": {"tool": "search"}}) == (
        "tool",
        {"name": "search", "phase": "started"},
    )
    assert _normalize({"type": "tool.completed", "data": {"name": "search"}}) == (
        "tool",
        {"name": "search", "phase": "completed"},
    )


def test_normalize_completed_and_unknown():
    assert _normalize({"type": "run.completed", "data": {}}) == ("completed", {})
    assert _normalize({"type": "ping", "data": {}}) == ("keepalive", {})


def test_maybe_json():
    assert _maybe_json('{"a": 1}') == {"a": 1}
    assert _maybe_json("not json") == "not json"


async def test_first_turn_creates_session(aiohttp_client):
    fake = _FakeHermes()
    app = build_app(
        hermes=fake, remote_user_header="Remote-User", default_uid="household"
    )
    client = await aiohttp_client(app)

    resp = await client.post(
        "/api/chat", json={"input": "hello"}, headers={"Remote-User": "mdopp"}
    )
    body = await resp.json()
    assert resp.status == 200
    assert body["ok"] is True
    assert body["session_id"] == "sess-1"
    assert body["reply"] == "echo: hello"
    assert fake.created == ["mdopp"]
    assert fake.turns == [("sess-1", "hello")]


async def test_subsequent_turn_reuses_session(aiohttp_client):
    fake = _FakeHermes()
    app = build_app(
        hermes=fake, remote_user_header="Remote-User", default_uid="household"
    )
    client = await aiohttp_client(app)

    resp = await client.post(
        "/api/chat", json={"input": "again", "session_id": "existing"}
    )
    body = await resp.json()
    assert body["session_id"] == "existing"
    assert fake.created == []
    assert fake.turns == [("existing", "again")]


async def test_empty_input_rejected(aiohttp_client):
    fake = _FakeHermes()
    app = build_app(
        hermes=fake, remote_user_header="Remote-User", default_uid="household"
    )
    client = await aiohttp_client(app)

    resp = await client.post("/api/chat", json={"input": "   "})
    assert resp.status == 400
    assert fake.created == []


async def test_stream_creates_session_and_restreams(aiohttp_client):
    fake = _FakeHermes(
        events=[
            {"type": "assistant.delta", "data": {"delta": "He"}},
            {"type": "tool.started", "data": {"tool": "clock"}},
            {"type": "assistant.delta", "data": {"delta": "llo"}},
            {"type": "run.completed", "data": {}},
        ]
    )
    app = build_app(
        hermes=fake, remote_user_header="Remote-User", default_uid="household"
    )
    client = await aiohttp_client(app)

    resp = await client.post(
        "/api/chat/stream", json={"input": "hi"}, headers={"Remote-User": "mdopp"}
    )
    assert resp.status == 200
    assert resp.headers["Content-Type"] == "text/event-stream"
    body = await resp.text()

    assert "event: session" in body
    assert '"session_id": "sess-1"' in body
    assert '"text": "He"' in body and '"text": "llo"' in body
    assert '"name": "clock"' in body and '"phase": "started"' in body
    assert "event: completed" in body
    assert body.rstrip().endswith("data: {}")  # final 'done' frame
    assert fake.created == ["mdopp"]
    assert fake.turns == [("sess-1", "hi")]


async def test_stream_empty_input_rejected(aiohttp_client):
    fake = _FakeHermes()
    app = build_app(
        hermes=fake, remote_user_header="Remote-User", default_uid="household"
    )
    client = await aiohttp_client(app)

    resp = await client.post("/api/chat/stream", json={"input": "  "})
    assert resp.status == 400
    assert fake.created == []


def _two_user_store():
    return [
        {
            "id": "s-mdopp",
            "user_id": "mdopp",
            "title": "Groceries",
            "last_activity": "2026-06-05T10:00:00Z",
            "messages": [
                {"role": "user", "content": "buy milk"},
                {"role": "assistant", "content": "added"},
            ],
        },
        {
            "id": "s-lena",
            "user_id": "lena",
            "title": "Lena private",
            "last_activity": "2026-06-05T11:00:00Z",
            "messages": [{"role": "user", "content": "secret"}],
        },
    ]


async def test_list_sessions_scoped_to_user(aiohttp_client):
    fake = _FakeHermes(store=_two_user_store())
    app = build_app(
        hermes=fake, remote_user_header="Remote-User", default_uid="household"
    )
    client = await aiohttp_client(app)

    resp = await client.get("/api/sessions", headers={"Remote-User": "mdopp"})
    body = await resp.json()
    assert resp.status == 200
    ids = [s["id"] for s in body["sessions"]]
    assert ids == ["s-mdopp"]
    assert "s-lena" not in ids


async def test_create_session_returns_id(aiohttp_client):
    fake = _FakeHermes()
    app = build_app(
        hermes=fake, remote_user_header="Remote-User", default_uid="household"
    )
    client = await aiohttp_client(app)

    resp = await client.post("/api/sessions", headers={"Remote-User": "mdopp"})
    body = await resp.json()
    assert resp.status == 200
    assert body["session_id"] == "sess-1"
    assert fake.created == ["mdopp"]


async def test_get_own_session_returns_history(aiohttp_client):
    fake = _FakeHermes(store=_two_user_store())
    app = build_app(
        hermes=fake, remote_user_header="Remote-User", default_uid="household"
    )
    client = await aiohttp_client(app)

    resp = await client.get("/api/sessions/s-mdopp", headers={"Remote-User": "mdopp"})
    body = await resp.json()
    assert resp.status == 200
    assert body["session"]["id"] == "s-mdopp"
    assert body["session"]["messages"][0]["content"] == "buy milk"


async def test_cannot_open_other_users_session(aiohttp_client):
    """PRIVACY INVARIANT: user A must not open user B's session by id."""
    fake = _FakeHermes(store=_two_user_store())
    app = build_app(
        hermes=fake, remote_user_header="Remote-User", default_uid="household"
    )
    client = await aiohttp_client(app)

    # mdopp tries to open lena's session by guessing its id.
    resp = await client.get("/api/sessions/s-lena", headers={"Remote-User": "mdopp"})
    body = await resp.json()
    assert resp.status == 404
    assert body["ok"] is False

    # And lena's session never shows in mdopp's list.
    resp = await client.get("/api/sessions", headers={"Remote-User": "mdopp"})
    listed = await resp.json()
    assert all(s["id"] != "s-lena" for s in listed["sessions"])

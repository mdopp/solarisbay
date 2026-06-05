"""Tests for uid mapping and the create-then-chat session flow."""

from __future__ import annotations

from oscar_chat.hermes import _extract_reply, _extract_session_id
from oscar_chat.server import build_app, resolve_uid


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
    def __init__(self):
        self.created = []
        self.turns = []

    async def create_session(self, uid):
        self.created.append(uid)
        return "sess-1"

    async def chat(self, session_id, text):
        self.turns.append((session_id, text))
        return f"echo: {text}"


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

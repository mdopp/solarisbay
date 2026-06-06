"""Tests for uid mapping and the create-then-chat session flow."""

from __future__ import annotations

from solilos_chat import personalities, skills
from solilos_chat.hermes import (
    _extract_messages,
    _extract_reply,
    _extract_session_id,
    _iter_session_items,
    _maybe_json,
    _session_owner,
    _session_summary,
)
from solilos_chat.server import (
    _normalize,
    _title_from,
    build_app,
    is_admin,
    resolve_uid,
)


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
        self.created_prompts = []
        self.turns = []
        self.titles = []
        self.deleted = []
        self._events = events or []
        # store: list of {id, user_id, title, last_activity, messages}
        self._store = store or []

    async def create_session(self, uid, system_prompt=None):
        self.created.append(uid)
        self.created_prompts.append(system_prompt or "")
        return "sess-1"

    async def set_title(self, session_id, title):
        self.titles.append((session_id, title))

    async def delete_session(self, session_id):
        self.deleted.append(session_id)
        return True

    async def list_toolsets(self):
        return [
            {
                "name": "web",
                "label": "Web",
                "description": "search",
                "enabled": True,
                "configured": True,
                "tools": ["web_search"],
            }
        ]

    async def chat(self, session_id, text):
        self.turns.append((session_id, text))
        return f"echo: {text}"

    async def chat_stream(self, session_id, text):
        self.turns.append((session_id, text))
        for event in self._events:
            yield event

    async def list_sessions(self, uid):
        # Mirror the real client: list-all (no per-resident filter) until #153.
        return [
            {
                "id": s["id"],
                "title": s.get("title", ""),
                "last_activity": s.get("last_activity", ""),
            }
            for s in self._store
        ]

    async def get_session(self, session_id, uid):
        # Mirror the real client: open any session (no ownership 404) until #153.
        for s in self._store:
            if s["id"] == session_id:
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
    # Real list-item shape: title set, epoch `last_active`, `preview`.
    summ = _session_summary(
        {
            "id": "x",
            "title": "Trip",
            "preview": "plan the trip",
            "last_active": 1780677907.7,
            "started_at": 1780677881.8,
        }
    )
    assert summ == {
        "id": "x",
        "title": "Trip",
        "preview": "plan the trip",
        "last_activity": "1780677907.7",
        "input_tokens": None,
        "output_tokens": None,
        "message_count": None,
        "estimated_cost_usd": None,
    }


def test_session_summary_null_title_surfaces_preview():
    # Chat-created sessions have title:null; the preview carries the label.
    summ = _session_summary(
        {"id": "y", "title": None, "preview": "buy milk", "started_at": 1780677881.8}
    )
    assert summ["title"] == ""
    assert summ["preview"] == "buy milk"
    assert summ["last_activity"] == "1780677881.8"


def test_extract_messages_data_envelope():
    # The real /messages payload: {"object": "list", "data": [...]}.
    body = {
        "object": "list",
        "data": [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": [{"text": "he"}, {"text": "llo"}]},
            {"role": "system", "content": ""},
        ],
    }
    assert _extract_messages(body) == [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
    ]
    assert _extract_messages({}) == []
    # Tolerate a bare `messages` key too.
    assert _extract_messages({"messages": [{"role": "user", "content": "yo"}]}) == [
        {"role": "user", "content": "yo"}
    ]


def test_title_from_first_message():
    assert _title_from("buy milk") == "buy milk"
    assert _title_from("  hello   there  ") == "hello there"
    long = "a" * 80
    out = _title_from(long)
    assert out.endswith("…") and len(out) <= 58
    assert _title_from("") == ""


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
    # First turn derives + persists a title from the user's message.
    assert fake.titles == [("sess-1", "hello")]


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
    # Reusing a session never re-titles it.
    assert fake.titles == []


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
    assert fake.titles == [("sess-1", "hi")]


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


async def test_list_sessions_returns_all(aiohttp_client):
    # Single-resident reality: list-all. Per-resident isolation -> #153
    # (Hermes v0.15.1 stores user_id:null, so no owner-filter is possible yet).
    fake = _FakeHermes(store=_two_user_store())
    app = build_app(
        hermes=fake, remote_user_header="Remote-User", default_uid="household"
    )
    client = await aiohttp_client(app)

    resp = await client.get("/api/sessions", headers={"Remote-User": "mdopp"})
    body = await resp.json()
    assert resp.status == 200
    ids = {s["id"] for s in body["sessions"]}
    assert ids == {"s-mdopp", "s-lena"}


async def test_delete_session(aiohttp_client):
    fake = _FakeHermes(store=_two_user_store())
    app = build_app(
        hermes=fake, remote_user_header="Remote-User", default_uid="household"
    )
    client = await aiohttp_client(app)

    resp = await client.delete(
        "/api/sessions/s-mdopp", headers={"Remote-User": "mdopp"}
    )
    body = await resp.json()
    assert resp.status == 200
    assert body == {"ok": True}
    assert fake.deleted == ["s-mdopp"]


async def test_whoami_reports_version(aiohttp_client):
    fake = _FakeHermes()
    app = build_app(
        hermes=fake, remote_user_header="Remote-User", default_uid="household"
    )
    client = await aiohttp_client(app)
    body = await (await client.get("/api/whoami")).json()
    assert "version" in body  # may be '' offline, but the key is always present


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


async def test_open_any_session_single_resident(aiohttp_client):
    # Single-resident reality: any listed session opens (no ownership 404).
    # Per-resident isolation is intentionally deferred -> #153 (Hermes v0.15.1
    # stores user_id:null, so the proxy cannot scope by resident yet); this
    # must be restored before multi-resident chat.
    fake = _FakeHermes(store=_two_user_store())
    app = build_app(
        hermes=fake, remote_user_header="Remote-User", default_uid="household"
    )
    client = await aiohttp_client(app)

    resp = await client.get("/api/sessions/s-lena", headers={"Remote-User": "mdopp"})
    body = await resp.json()
    assert resp.status == 200
    assert body["session"]["id"] == "s-lena"
    assert body["session"]["messages"][0]["content"] == "secret"

    # An unknown id still 404s (Hermes itself doesn't have it).
    resp = await client.get("/api/sessions/nope", headers={"Remote-User": "mdopp"})
    assert resp.status == 404


# --- Admin gate -----------------------------------------------------------


def test_is_admin_membership():
    req = _FakeRequest({"Remote-Groups": "family,admins"})
    assert is_admin(req, "Remote-Groups", "admins") is True


def test_is_admin_absent_or_other_group():
    assert is_admin(_FakeRequest({}), "Remote-Groups", "admins") is False
    assert (
        is_admin(_FakeRequest({"Remote-Groups": "family"}), "Remote-Groups", "admins")
        is False
    )
    # Substring of another group must not match (set membership, not `in`).
    assert (
        is_admin(
            _FakeRequest({"Remote-Groups": "superadmins"}), "Remote-Groups", "admins"
        )
        is False
    )


async def test_whoami_reports_uid_and_admin(aiohttp_client):
    fake = _FakeHermes()
    app = build_app(
        hermes=fake, remote_user_header="Remote-User", default_uid="household"
    )
    client = await aiohttp_client(app)

    resp = await client.get(
        "/api/whoami", headers={"Remote-User": "mdopp", "Remote-Groups": "admins"}
    )
    body = await resp.json()
    assert body["ok"] is True and body["uid"] == "mdopp" and body["is_admin"] is True

    resp = await client.get(
        "/api/whoami", headers={"Remote-User": "cdopp", "Remote-Groups": "family"}
    )
    body = await resp.json()
    assert body["uid"] == "cdopp" and body["is_admin"] is False


# --- Personalities --------------------------------------------------------


def test_personalities_catalog_hides_prompts():
    cat = personalities.catalog()
    assert any(p["id"] == "sol" for p in cat)
    for p in cat:
        assert set(p) == {"id", "label", "description"}  # no system_prompt leaked


def test_system_prompt_for():
    assert personalities.system_prompt_for("sol") == ""  # default = no overlay
    assert personalities.system_prompt_for(None) == ""
    assert personalities.system_prompt_for("concise")  # non-empty overlay
    assert personalities.system_prompt_for("nope") == ""  # unknown = default


async def test_list_personalities_endpoint(aiohttp_client):
    fake = _FakeHermes()
    app = build_app(
        hermes=fake, remote_user_header="Remote-User", default_uid="household"
    )
    client = await aiohttp_client(app)
    resp = await client.get("/api/personalities")
    body = await resp.json()
    assert resp.status == 200
    assert {p["id"] for p in body["personalities"]} >= {"sol", "concise"}


async def test_chat_passes_personality_system_prompt(aiohttp_client):
    fake = _FakeHermes()
    app = build_app(
        hermes=fake, remote_user_header="Remote-User", default_uid="household"
    )
    client = await aiohttp_client(app)

    resp = await client.post(
        "/api/chat", json={"input": "hi", "personality": "concise"}
    )
    assert resp.status == 200
    assert fake.created_prompts == [personalities.system_prompt_for("concise")]


async def test_chat_default_personality_no_overlay(aiohttp_client):
    fake = _FakeHermes()
    app = build_app(
        hermes=fake, remote_user_header="Remote-User", default_uid="household"
    )
    client = await aiohttp_client(app)

    resp = await client.post("/api/chat", json={"input": "hi"})
    assert resp.status == 200
    assert fake.created_prompts == [""]


async def test_create_session_with_personality(aiohttp_client):
    fake = _FakeHermes()
    app = build_app(
        hermes=fake, remote_user_header="Remote-User", default_uid="household"
    )
    client = await aiohttp_client(app)

    resp = await client.post("/api/sessions", json={"personality": "teacher"})
    assert resp.status == 200
    assert fake.created_prompts == [personalities.system_prompt_for("teacher")]


# --- Skills (filesystem) --------------------------------------------------


def _write_skill(root, dir_name, name, description, body):
    d = root / dir_name
    d.mkdir()
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\nversion: 1\n---\n\n{body}\n",
        encoding="utf-8",
    )


def test_skills_list_and_read_from_disk(tmp_path):
    _write_skill(tmp_path, "status", "sol-status", "Health at a glance", "# Status\nok")
    _write_skill(tmp_path, "notes", "sol-notes", "Search notes", "# Notes\nbody")
    (tmp_path / "README.md").write_text("not a skill", encoding="utf-8")  # ignored

    listed = skills.list_skills(tmp_path)
    assert [s["id"] for s in listed] == ["notes", "status"]  # sorted by name
    assert listed[1] == {
        "id": "status",
        "name": "sol-status",
        "description": "Health at a glance",
    }

    one = skills.read_skill(tmp_path, "status")
    assert one["name"] == "sol-status"
    assert one["body"].strip() == "# Status\nok"


def test_skills_missing_dir_and_traversal(tmp_path):
    assert skills.list_skills(tmp_path / "nope") == []
    assert skills.read_skill(tmp_path, "../etc") is None
    assert skills.read_skill(tmp_path, "nope") is None


async def test_skills_endpoints(aiohttp_client, tmp_path):
    _write_skill(tmp_path, "status", "sol-status", "Health", "# Status\nrendered me")
    fake = _FakeHermes()
    app = build_app(
        hermes=fake,
        remote_user_header="Remote-User",
        default_uid="household",
        skills_dir=str(tmp_path),
    )
    client = await aiohttp_client(app)

    resp = await client.get("/api/skills")
    body = await resp.json()
    assert resp.status == 200
    assert body["skills"][0]["name"] == "sol-status"

    resp = await client.get("/api/skills/status")
    body = await resp.json()
    assert "rendered me" in body["skill"]["body"]

    resp = await client.get("/api/skills/missing")
    assert resp.status == 404


# --- Soul -----------------------------------------------------------------


async def test_soul_endpoint_reads_via_agent(aiohttp_client, monkeypatch):
    from solilos_chat import server as server_mod

    async def fake_get(url, token):
        return "# Sol\nI am the soul."

    monkeypatch.setattr(server_mod, "_agent_get_soul", fake_get)
    app = build_app(
        hermes=_FakeHermes(), remote_user_header="Remote-User", default_uid="household"
    )
    client = await aiohttp_client(app)
    resp = await client.get("/api/soul")
    body = await resp.json()
    assert resp.status == 200
    assert body["soul"]["content"] == "# Sol\nI am the soul."


async def test_soul_endpoint_agent_unavailable(aiohttp_client, monkeypatch):
    from solilos_chat import server as server_mod

    async def fake_get(url, token):
        return None

    monkeypatch.setattr(server_mod, "_agent_get_soul", fake_get)
    app = build_app(
        hermes=_FakeHermes(), remote_user_header="Remote-User", default_uid="household"
    )
    client = await aiohttp_client(app)
    resp = await client.get("/api/soul")
    assert resp.status == 502


async def test_toolsets_endpoint(aiohttp_client):
    app = build_app(
        hermes=_FakeHermes(), remote_user_header="Remote-User", default_uid="household"
    )
    client = await aiohttp_client(app)
    body = await (await client.get("/api/toolsets")).json()
    assert body["ok"] is True
    assert body["toolsets"][0]["name"] == "web"


async def test_whoami_reports_context_window(aiohttp_client):
    app = build_app(
        hermes=_FakeHermes(),
        remote_user_header="Remote-User",
        default_uid="household",
        context_window=4096,
    )
    client = await aiohttp_client(app)
    body = await (await client.get("/api/whoami")).json()
    assert body["context_window"] == 4096


# --- Soul edit (admin, proxied to the config sidecar) ---------------------


async def test_put_soul_admin_proxies_to_agent(aiohttp_client, monkeypatch):
    from solilos_chat import server as server_mod

    calls = []

    async def fake_agent(url, token, content):
        calls.append((url, token, content))
        return True

    monkeypatch.setattr(server_mod, "_agent_put_soul", fake_agent)
    app = build_app(
        hermes=_FakeHermes(),
        remote_user_header="Remote-User",
        default_uid="household",
        config_agent_url="http://agent:8650",
        agent_token="k",
    )
    client = await aiohttp_client(app)

    resp = await client.put(
        "/api/soul", json={"content": "# Sol\nnew"}, headers={"Remote-Groups": "admins"}
    )
    assert resp.status == 200
    assert calls == [("http://agent:8650", "k", "# Sol\nnew")]


async def test_put_soul_non_admin_forbidden_no_agent_call(aiohttp_client, monkeypatch):
    from solilos_chat import server as server_mod

    called = []

    async def fake_agent(url, token, content):
        called.append(1)
        return True

    monkeypatch.setattr(server_mod, "_agent_put_soul", fake_agent)
    app = build_app(
        hermes=_FakeHermes(), remote_user_header="Remote-User", default_uid="household"
    )
    client = await aiohttp_client(app)

    resp = await client.put(
        "/api/soul", json={"content": "x"}, headers={"Remote-Groups": "family"}
    )
    assert resp.status == 403
    assert called == []  # the write never reached the agent


async def test_put_soul_empty_rejected(aiohttp_client):
    app = build_app(
        hermes=_FakeHermes(), remote_user_header="Remote-User", default_uid="household"
    )
    client = await aiohttp_client(app)
    resp = await client.put(
        "/api/soul", json={"content": "  "}, headers={"Remote-Groups": "admins"}
    )
    assert resp.status == 400


async def test_put_soul_agent_failure_is_502(aiohttp_client, monkeypatch):
    from solilos_chat import server as server_mod

    async def fake_agent(url, token, content):
        return False

    monkeypatch.setattr(server_mod, "_agent_put_soul", fake_agent)
    app = build_app(
        hermes=_FakeHermes(), remote_user_header="Remote-User", default_uid="household"
    )
    client = await aiohttp_client(app)
    resp = await client.put(
        "/api/soul", json={"content": "x"}, headers={"Remote-Groups": "admins"}
    )
    assert resp.status == 502


# --- Model switch (admin, proxied to the config sidecar) ------------------


async def test_get_model_admin(aiohttp_client, monkeypatch):
    from solilos_chat import server as server_mod

    async def fake_get(url, token):
        return {"current": "gemma4:e4b", "available": ["gemma4:e4b", "llama3:8b"]}

    monkeypatch.setattr(server_mod, "_agent_get_model", fake_get)
    app = build_app(
        hermes=_FakeHermes(), remote_user_header="Remote-User", default_uid="household"
    )
    client = await aiohttp_client(app)
    resp = await client.get("/api/model", headers={"Remote-Groups": "admins"})
    body = await resp.json()
    assert resp.status == 200
    assert body["current"] == "gemma4:e4b"
    assert body["available"] == ["gemma4:e4b", "llama3:8b"]


async def test_get_model_non_admin_forbidden(aiohttp_client):
    app = build_app(
        hermes=_FakeHermes(), remote_user_header="Remote-User", default_uid="household"
    )
    client = await aiohttp_client(app)
    resp = await client.get("/api/model", headers={"Remote-Groups": "family"})
    assert resp.status == 403


async def test_put_model_admin_proxies(aiohttp_client, monkeypatch):
    from solilos_chat import server as server_mod

    calls = []

    async def fake_put(url, token, model):
        calls.append((url, token, model))
        return {"ok": True, "restarted": True}

    monkeypatch.setattr(server_mod, "_agent_put_model", fake_put)
    app = build_app(
        hermes=_FakeHermes(),
        remote_user_header="Remote-User",
        default_uid="household",
        config_agent_url="http://agent:8650",
        agent_token="k",
    )
    client = await aiohttp_client(app)
    resp = await client.put(
        "/api/model", json={"model": "llama3:8b"}, headers={"Remote-Groups": "admins"}
    )
    body = await resp.json()
    assert resp.status == 200
    assert body == {"ok": True, "restarted": True}
    assert calls == [("http://agent:8650", "k", "llama3:8b")]


async def test_put_model_non_admin_forbidden_no_call(aiohttp_client, monkeypatch):
    from solilos_chat import server as server_mod

    called = []

    async def fake_put(url, token, model):
        called.append(1)
        return {"ok": True}

    monkeypatch.setattr(server_mod, "_agent_put_model", fake_put)
    app = build_app(
        hermes=_FakeHermes(), remote_user_header="Remote-User", default_uid="household"
    )
    client = await aiohttp_client(app)
    resp = await client.put(
        "/api/model", json={"model": "x"}, headers={"Remote-Groups": "family"}
    )
    assert resp.status == 403
    assert called == []


async def test_put_model_agent_failure_is_502(aiohttp_client, monkeypatch):
    from solilos_chat import server as server_mod

    async def fake_put(url, token, model):
        return None

    monkeypatch.setattr(server_mod, "_agent_put_model", fake_put)
    app = build_app(
        hermes=_FakeHermes(), remote_user_header="Remote-User", default_uid="household"
    )
    client = await aiohttp_client(app)
    resp = await client.put(
        "/api/model", json={"model": "x"}, headers={"Remote-Groups": "admins"}
    )
    assert resp.status == 502


# --- Skill edit (admin) ---------------------------------------------------


def test_read_skill_exposes_raw(tmp_path):
    _write_skill(tmp_path, "status", "sol-status", "Health", "# Status\nok")
    one = skills.read_skill(tmp_path, "status")
    # raw is the full file (frontmatter + body) the editor loads.
    assert one["raw"].startswith("---\nname: sol-status")
    assert "# Status" in one["raw"]


def test_write_skill_body_only_no_restart(tmp_path):
    _write_skill(tmp_path, "status", "sol-status", "Health", "# Status\nold")
    new = (
        "---\nname: sol-status\ndescription: Health\nversion: 1\n---\n\n# Status\nnew\n"
    )
    result = skills.write_skill(tmp_path, "status", new)
    assert result == {"id": "status", "frontmatter_changed": False}
    assert skills.read_skill(tmp_path, "status")["body"].strip() == "# Status\nnew"


def test_write_skill_frontmatter_change_flags_restart(tmp_path):
    _write_skill(tmp_path, "status", "sol-status", "Health", "# Status\nok")
    new = (
        "---\nname: sol-status\ndescription: Changed\nversion: 1\n---\n\n# Status\nok\n"
    )
    result = skills.write_skill(tmp_path, "status", new)
    assert result["frontmatter_changed"] is True
    assert skills.read_skill(tmp_path, "status")["description"] == "Changed"


def test_write_skill_rejects_missing_and_traversal(tmp_path):
    _write_skill(tmp_path, "status", "sol-status", "Health", "# Status\nok")
    assert skills.write_skill(tmp_path, "nope", "x") is None
    assert skills.write_skill(tmp_path, "../etc", "x") is None


def _skill_app(fake, tmp_path):
    return build_app(
        hermes=fake,
        remote_user_header="Remote-User",
        default_uid="household",
        skills_dir=str(tmp_path),
    )


async def test_put_skill_admin_saves(aiohttp_client, tmp_path):
    _write_skill(tmp_path, "status", "sol-status", "Health", "# Status\nold")
    client = await aiohttp_client(_skill_app(_FakeHermes(), tmp_path))
    new = (
        "---\nname: sol-status\ndescription: Health\nversion: 1\n---\n\n# Status\nnew\n"
    )

    resp = await client.put(
        "/api/skills/status",
        json={"content": new},
        headers={"Remote-User": "mdopp", "Remote-Groups": "admins"},
    )
    body = await resp.json()
    assert resp.status == 200
    assert body == {"ok": True, "restart_needed": False}
    assert skills.read_skill(tmp_path, "status")["body"].strip() == "# Status\nnew"


async def test_put_skill_frontmatter_change_signals_restart(aiohttp_client, tmp_path):
    _write_skill(tmp_path, "status", "sol-status", "Health", "# Status\nok")
    client = await aiohttp_client(_skill_app(_FakeHermes(), tmp_path))
    new = "---\nname: sol-status\ndescription: New\nversion: 1\n---\n\n# Status\nok\n"

    resp = await client.put(
        "/api/skills/status",
        json={"content": new},
        headers={"Remote-Groups": "admins"},
    )
    body = await resp.json()
    assert body["restart_needed"] is True


async def test_put_skill_non_admin_forbidden_and_unchanged(aiohttp_client, tmp_path):
    _write_skill(tmp_path, "status", "sol-status", "Health", "# Status\nkeep")
    client = await aiohttp_client(_skill_app(_FakeHermes(), tmp_path))

    resp = await client.put(
        "/api/skills/status",
        json={"content": "---\nname: x\n---\nhacked"},
        headers={"Remote-User": "cdopp", "Remote-Groups": "family"},
    )
    assert resp.status == 403
    # The file must be untouched by a rejected write.
    assert skills.read_skill(tmp_path, "status")["body"].strip() == "# Status\nkeep"


async def test_put_skill_missing_and_empty(aiohttp_client, tmp_path):
    _write_skill(tmp_path, "status", "sol-status", "Health", "# Status\nok")
    client = await aiohttp_client(_skill_app(_FakeHermes(), tmp_path))
    admin = {"Remote-Groups": "admins"}

    resp = await client.put("/api/skills/missing", json={"content": "x"}, headers=admin)
    assert resp.status == 404

    resp = await client.put("/api/skills/status", json={"content": "  "}, headers=admin)
    assert resp.status == 400

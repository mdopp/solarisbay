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
        self._events = events or []
        # store: list of {id, user_id, title, last_activity, messages}
        self._store = store or []

    async def create_session(self, uid, system_prompt=None):
        self.created.append(uid)
        self.created_prompts.append(system_prompt or "")
        return "sess-1"

    async def set_title(self, session_id, title):
        self.titles.append((session_id, title))

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
    assert body == {"ok": True, "uid": "mdopp", "is_admin": True}

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


async def test_soul_endpoint(aiohttp_client, tmp_path):
    soul = tmp_path / "SOUL.md"
    soul.write_text("# Sol\nI am the soul.", encoding="utf-8")
    fake = _FakeHermes()
    app = build_app(
        hermes=fake,
        remote_user_header="Remote-User",
        default_uid="household",
        soul_path=str(soul),
    )
    client = await aiohttp_client(app)

    resp = await client.get("/api/soul")
    body = await resp.json()
    assert resp.status == 200
    assert body["soul"]["content"] == "# Sol\nI am the soul."


async def test_soul_endpoint_missing(aiohttp_client, tmp_path):
    fake = _FakeHermes()
    app = build_app(
        hermes=fake,
        remote_user_header="Remote-User",
        default_uid="household",
        soul_path=str(tmp_path / "absent.md"),
    )
    client = await aiohttp_client(app)
    resp = await client.get("/api/soul")
    assert resp.status == 404

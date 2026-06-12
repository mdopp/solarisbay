"""Tests for the Ollama-compatible facade + the engine's stateless respond().

The facade is what HA's `ollama` integration and the voice-gatekeeper speak:
GET /ollama/api/tags for config-flow validation, POST /ollama/api/chat for
turns (NDJSON stream or single JSON). respond() runs the same agent loop
statelessly — caller-owned history, nothing persisted to the store.
"""

from __future__ import annotations

import json
import sqlite3

import pytest
from solilos_chat.engine.client import EngineClient, EngineProfile
from solilos_chat.engine.ollama import ChatResult
from solilos_chat.engine.tools import Tool, Toolbox
from solilos_chat.engine.trace import TraceRecorder
from solilos_chat.server import build_app

from tests.test_engine import _SCHEMA, FakeOllama


@pytest.fixture
def db(tmp_path) -> str:
    path = str(tmp_path / "solilos.db")
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA)
    conn.commit()
    conn.close()
    return path


@pytest.fixture
def soul(tmp_path) -> str:
    path = tmp_path / "SOUL.md"
    path.write_text("Du bist Sol.", encoding="utf-8")
    return str(path)


def _engine(db, soul, results, tools=None, name="household"):
    fake = FakeOllama(results)
    client = EngineClient(
        EngineProfile(
            name=name,
            model="gemma4:e2b",
            soul_path=soul,
            toolbox=Toolbox(tools or []),
        ),
        db_path=db,
        ollama=fake,
        recorder=TraceRecorder(),
        context_window=32768,
    )
    return client, fake


# -- respond() -------------------------------------------------------------


async def test_respond_is_stateless_and_folds_system(db, soul):
    client, fake = _engine(
        db, soul, [ChatResult(content="Klar.", prompt_tokens=10, completion_tokens=2)]
    )
    messages = [
        {"role": "system", "content": "Antworte kurz."},
        {"role": "user", "content": "Licht an"},
    ]
    events = [e async for e in client.respond(messages, uid="michael")]
    assert events[-1]["type"] == "run.completed"
    sent = fake.calls[0]["messages"]
    # One folded system block: soul first, HA's prompt after.
    assert sent[0]["role"] == "system"
    assert sent[0]["content"].startswith("Du bist Sol.")
    assert "Antworte kurz." in sent[0]["content"]
    assert sum(1 for m in sent if m["role"] == "system") == 1
    # Time hint rides the last user message, not the system block.
    assert sent[-1]["content"].startswith("[Aktuelle Zeit:")
    assert sent[-1]["content"].endswith("Licht an")
    # Nothing persisted: the store has no sessions.
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM engine_sessions").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM engine_messages").fetchone()[0] == 0
    conn.close()


async def test_respond_runs_tool_loop(db, soul):
    seen = []

    async def handler(args):
        seen.append(args)
        return '{"ok": true}'

    tool = Tool(
        name="ha_call_service",
        description="x",
        parameters={"type": "object", "properties": {}},
        handler=handler,
    )
    client, fake = _engine(
        db,
        soul,
        [
            ChatResult(
                content="",
                tool_calls=[
                    {
                        "function": {
                            "name": "ha_call_service",
                            "arguments": {"entity_id": "light.buero"},
                        }
                    }
                ],
                prompt_tokens=10,
            ),
            ChatResult(content="Erledigt.", prompt_tokens=12, completion_tokens=2),
        ],
        tools=[tool],
    )
    events = [
        e
        async for e in client.respond(
            [{"role": "user", "content": "mach das büro licht an"}], uid="michael"
        )
    ]
    assert seen == [{"entity_id": "light.buero"}]
    kinds = [e["type"] for e in events]
    assert "tool.started" in kinds and "tool.completed" in kinds
    final = events[-1]["data"]["messages"][-1]["content"]
    assert final == "Erledigt."
    assert len(fake.calls) == 2


# -- facade routes -----------------------------------------------------------


def _app(db, soul, results, api_key=""):
    household, fake = _engine(db, soul, results)
    deep, _ = _engine(db, soul, [], name="sol-deep")
    app = build_app(
        hermes=household,
        hermes_deep=deep,
        remote_user_header="Remote-User",
        default_uid="household",
        solilos_db_path=db,
        api_key=api_key,
    )
    return app, fake


async def test_tags_lists_profiles(aiohttp_client, db, soul):
    app, _ = _app(db, soul, [])
    client = await aiohttp_client(app)
    body = await (await client.get("/ollama/api/tags")).json()
    names = [m["model"] for m in body["models"]]
    assert names == ["sol", "sol-deep"]


async def test_tags_requires_bearer_when_key_set(aiohttp_client, db, soul):
    app, _ = _app(db, soul, [], api_key="secret")
    client = await aiohttp_client(app)
    assert (await client.get("/ollama/api/tags")).status == 401
    ok = await client.get(
        "/ollama/api/tags", headers={"Authorization": "Bearer secret"}
    )
    assert ok.status == 200


async def test_chat_stream_ndjson(aiohttp_client, db, soul):
    app, _ = _app(
        db,
        soul,
        [ChatResult(content="Hallo zurück!", prompt_tokens=10, completion_tokens=3)],
    )
    client = await aiohttp_client(app)
    resp = await client.post(
        "/ollama/api/chat",
        json={"model": "sol", "messages": [{"role": "user", "content": "Hallo"}]},
    )
    assert resp.status == 200
    lines = [json.loads(line) for line in (await resp.text()).strip().splitlines()]
    assert lines[-1]["done"] is True
    assert lines[-1]["done_reason"] == "stop"
    content = "".join(line["message"]["content"] for line in lines)
    assert "Hallo" in content


async def test_chat_non_stream_single_json(aiohttp_client, db, soul):
    app, _ = _app(
        db,
        soul,
        [ChatResult(content="Erledigt.", prompt_tokens=10, completion_tokens=2)],
    )
    client = await aiohttp_client(app)
    resp = await client.post(
        "/ollama/api/chat",
        json={
            "model": "sol",
            "stream": False,
            "messages": [{"role": "user", "content": "Licht an"}],
            "user": "michael",
        },
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["done"] is True
    assert body["message"]["content"] == "Erledigt."


async def test_chat_unknown_model_404(aiohttp_client, db, soul):
    app, _ = _app(db, soul, [])
    client = await aiohttp_client(app)
    resp = await client.post(
        "/ollama/api/chat",
        json={"model": "gpt-5", "messages": [{"role": "user", "content": "x"}]},
    )
    assert resp.status == 404


async def test_chat_latest_suffix_resolves(aiohttp_client, db, soul):
    app, _ = _app(
        db, soul, [ChatResult(content="ok", prompt_tokens=1, completion_tokens=1)]
    )
    client = await aiohttp_client(app)
    resp = await client.post(
        "/ollama/api/chat",
        json={
            "model": "sol:latest",
            "stream": False,
            "messages": [{"role": "user", "content": "x"}],
        },
    )
    assert resp.status == 200

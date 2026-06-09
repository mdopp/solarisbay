"""Tests for the persisted per-message trace store + endpoint (#306).

Covers the `session_traces` store (persist/list, ordering, per-resident scope,
idempotent re-persist, missing-db degradation) and the turn-time correlation:
the server pulls the proxy's `/__traces__` calls in the turn window, assigns a
per-message trace_id, persists them, and serves them at
`GET /api/sessions/<id>/trace` reopen-consistently.
"""

from __future__ import annotations

import sqlite3
import time

from aiohttp import web

from solilos_chat import trace_store
from solilos_chat.server import build_app

# The schema the 0007 migration creates, replayed locally so the store + endpoint
# tests run against a real sqlite db without alembic.
_SCHEMA = """
CREATE TABLE session_traces (
  session_id        TEXT NOT NULL,
  trace_id          TEXT NOT NULL,
  step_order        INTEGER NOT NULL,
  owner_uid         TEXT NOT NULL,
  model             TEXT,
  wall_s            REAL,
  prompt_tokens     INTEGER,
  completion_tokens INTEGER,
  context_free      INTEGER,
  finish_reason     TEXT,
  n_tools           INTEGER,
  detail_id         INTEGER,
  created_at        TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (session_id, trace_id, step_order)
);
CREATE INDEX session_traces_session_idx
  ON session_traces (session_id, owner_uid);
"""


def _db(tmp_path) -> str:
    path = str(tmp_path / "solilos.db")
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA)
    conn.commit()
    conn.close()
    return path


def _step(model="gemma4:e2b", detail_id=0, **over):
    base = {
        "model": model,
        "wall_s": 1.2,
        "prompt_tokens": 100,
        "completion_tokens": 10,
        "context_free": 30000,
        "finish_reason": "stop",
        "n_tools": 5,
        "detail_id": detail_id,
    }
    base.update(over)
    return base


def test_persist_then_list_keeps_step_order(tmp_path):
    db = _db(tmp_path)
    steps = [_step(detail_id=0, finish_reason="tool_calls"), _step(detail_id=1)]
    trace_store.persist_trace(db, "sess-1", "tr-a", "mdopp", steps)
    got = trace_store.list_session_trace(db, "sess-1", "mdopp")
    assert [s["step_order"] for s in got] == [0, 1]
    assert [s["detail_id"] for s in got] == [0, 1]
    assert got[0]["finish_reason"] == "tool_calls"
    assert got[1]["model"] == "gemma4:e2b"


def test_multiple_turns_list_in_chronological_order(tmp_path):
    # Two turns (distinct trace_ids) persisted in order — the reopen reads them
    # back turn-then-step, matching the live order (#306 acceptance).
    db = _db(tmp_path)
    trace_store.persist_trace(db, "sess-1", "tr-1", "mdopp", [_step(detail_id=0)])
    trace_store.persist_trace(
        db, "sess-1", "tr-2", "mdopp", [_step(detail_id=1), _step(detail_id=2)]
    )
    got = trace_store.list_session_trace(db, "sess-1", "mdopp")
    assert [s["trace_id"] for s in got] == ["tr-1", "tr-2", "tr-2"]
    assert [s["detail_id"] for s in got] == [0, 1, 2]


def test_re_persist_same_trace_id_replaces(tmp_path):
    db = _db(tmp_path)
    trace_store.persist_trace(db, "sess-1", "tr-a", "mdopp", [_step(detail_id=9)])
    trace_store.persist_trace(
        db, "sess-1", "tr-a", "mdopp", [_step(detail_id=0), _step(detail_id=1)]
    )
    got = trace_store.list_session_trace(db, "sess-1", "mdopp")
    assert [s["detail_id"] for s in got] == [0, 1]


def test_scopes_to_resident(tmp_path):
    db = _db(tmp_path)
    trace_store.persist_trace(db, "sess-1", "tr-a", "mdopp", [_step(detail_id=0)])
    assert trace_store.list_session_trace(db, "sess-1", "lena") == []
    assert len(trace_store.list_session_trace(db, "sess-1", "mdopp")) == 1


def test_missing_db_degrades(tmp_path):
    missing = str(tmp_path / "absent.db")
    # No-op write, empty read — never raises.
    trace_store.persist_trace(missing, "sess-1", "tr-a", "mdopp", [_step()])
    assert trace_store.list_session_trace(missing, "sess-1", "mdopp") == []


def test_empty_steps_is_noop(tmp_path):
    db = _db(tmp_path)
    trace_store.persist_trace(db, "sess-1", "tr-a", "mdopp", [])
    assert trace_store.list_session_trace(db, "sess-1", "mdopp") == []


class _FakeHermes:
    async def create_session(self, uid, system_prompt=None, **kw):
        return "sess-1"

    async def set_title(self, *a, **k):
        pass

    async def chat(self, session_id, text, images=None, reasoning_effort="none"):
        return f"echo: {text}"


async def _trace_proxy_app():
    """A stub for the trace proxy's `/__traces__` list (newest-first).

    The two in-window records are stamped with the server-side `time.time()` at
    request time — the proxy is hit *during* the turn (after Hermes, before t1),
    so they reliably fall inside the turn's `[t0, t1]` window. The third carries a
    fixed old `ts`, well before the turn, to assert the window filter drops it.
    """

    async def handle(_request):
        ts = time.time()
        return web.json_response(
            [
                {
                    "id": 2,
                    "ts": ts,
                    "model": "m",
                    "wall_s": 0.5,
                    "prompt_tokens": 90,
                    "completion_tokens": 5,
                    "context_free": 100,
                    "finish_reason": "stop",
                    "n_tools": 3,
                },
                {
                    "id": 1,
                    "ts": ts,
                    "model": "m",
                    "wall_s": 0.7,
                    "prompt_tokens": 80,
                    "completion_tokens": 4,
                    "context_free": 120,
                    "finish_reason": "tool_calls",
                    "n_tools": 3,
                },
                {
                    "id": 0,
                    "ts": ts - 3600.0,
                    "model": "m",
                    "wall_s": 0.1,
                    "prompt_tokens": 10,
                    "completion_tokens": 1,
                    "context_free": 0,
                    "finish_reason": "stop",
                    "n_tools": 0,
                },
            ]
        )

    app = web.Application()
    app.router.add_get("/__traces__", handle)
    return app


async def test_turn_persists_window_and_endpoint_serves_it(
    aiohttp_client, aiohttp_server, tmp_path
):
    db = _db(tmp_path)
    proxy = await aiohttp_server(await _trace_proxy_app())
    proxy_url = f"http://{proxy.host}:{proxy.port}"

    app = build_app(
        hermes=_FakeHermes(),
        remote_user_header="Remote-User",
        default_uid="household",
        attachments_dir=str(tmp_path / "att"),
        solilos_db_path=db,
        trace_proxy_url=proxy_url,
    )
    client = await aiohttp_client(app)

    r = await client.post(
        "/api/chat",
        json={"input": "Welche Lichter sind an?"},
        headers={"Remote-User": "mdopp"},
    )
    assert r.status == 200

    r = await client.get("/api/sessions/sess-1/trace", headers={"Remote-User": "mdopp"})
    body = await r.json()
    assert body["ok"] is True
    # Only the two in-window calls (the pre-window call is dropped), kept in the
    # list's order via the stable ts sort, each carrying the proxy id as
    # detail_id for the per-step content fetch.
    assert [s["detail_id"] for s in body["steps"]] == [2, 1]
    assert body["steps"][1]["finish_reason"] == "tool_calls"


async def test_trace_detail_proxied_to_proxy(aiohttp_client, aiohttp_server, tmp_path):
    # The browser can't reach the proxy port, so the chat server passes
    # /__traces__/<id> through to the trace proxy's detail endpoint (#307 panel
    # → #305 detail). Status + body are forwarded verbatim, including a 404 for
    # an evicted-from-the-ring detail id.
    async def detail(request):
        if request.match_info["tail"] == "7":
            return web.json_response(
                {"model": "m", "tools": [{"a": 1}], "messages": [], "final": "hi"}
            )
        return web.json_response({"error": "not found"}, status=404)

    proxy = web.Application()
    proxy.router.add_get("/__traces__/{tail}", detail)
    proxy_server = await aiohttp_server(proxy)
    proxy_url = f"http://{proxy_server.host}:{proxy_server.port}"

    app = build_app(
        hermes=_FakeHermes(),
        remote_user_header="Remote-User",
        default_uid="household",
        attachments_dir=str(tmp_path / "att"),
        solilos_db_path=_db(tmp_path),
        trace_proxy_url=proxy_url,
    )
    client = await aiohttp_client(app)

    r = await client.get("/__traces__/7", headers={"Remote-User": "mdopp"})
    assert r.status == 200
    body = await r.json()
    assert body["final"] == "hi"
    assert body["tools"] == [{"a": 1}]

    r = await client.get("/__traces__/999", headers={"Remote-User": "mdopp"})
    assert r.status == 404


async def test_endpoint_empty_when_no_trace(aiohttp_client, tmp_path):
    db = _db(tmp_path)
    app = build_app(
        hermes=_FakeHermes(),
        remote_user_header="Remote-User",
        default_uid="household",
        attachments_dir=str(tmp_path / "att"),
        solilos_db_path=db,
    )
    client = await aiohttp_client(app)
    r = await client.get("/api/sessions/none/trace", headers={"Remote-User": "mdopp"})
    body = await r.json()
    assert body == {"ok": True, "steps": []}

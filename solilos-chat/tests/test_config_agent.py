"""Tests for the privileged config sidecar (runs in the hermes pod)."""

from __future__ import annotations

import os

import aiohttp

from solilos_chat import config_agent
from solilos_chat.config_agent import (
    _diagnose_connect_error,
    _find_model_value,
    _parse_mcp_servers,
    _servicebay_mcp_creds,
    _set_model_in_config,
    build_app,
)

TOKEN = "secret-key"
AUTH = {"Authorization": f"Bearer {TOKEN}"}

SAMPLE_CONFIG = (
    "model:\n"
    "  provider: custom\n"
    "  model: gemma4:e4b\n"
    "  base_url: http://127.0.0.1:11434/v1\n"
    "memory:\n"
    "  provider: holographic\n"
    "mcp_servers:\n"
    "  ha-mcp:\n"
    '    url: "http://127.0.0.1:10100/mcp"\n'
    "  servicebay-mcp:\n"
    '    url: "http://127.0.0.1:5888/mcp"\n'
    "    headers:\n"
    '      Authorization: "Bearer sb_0a1b2c3d_ABCDEF234567"\n'
)


def _app(tmp_path):
    return build_app(token=TOKEN, soul_path=str(tmp_path / "SOUL.md"))


async def test_health_needs_no_auth(aiohttp_client, tmp_path):
    client = await aiohttp_client(_app(tmp_path))
    resp = await client.get("/health")
    assert resp.status == 200
    assert (await resp.json())["ok"] is True


async def test_put_soul_writes_with_auth(aiohttp_client, tmp_path):
    client = await aiohttp_client(_app(tmp_path))
    resp = await client.put("/soul", json={"content": "# Sol\nnew soul"}, headers=AUTH)
    assert resp.status == 200
    soul = tmp_path / "SOUL.md"
    assert soul.read_text(encoding="utf-8") == "# Sol\nnew soul"
    # World-readable so Hermes (a different uid in the shared pod) can read it.
    assert (os.stat(soul).st_mode & 0o777) == 0o644


async def test_put_soul_rejects_missing_auth(aiohttp_client, tmp_path):
    client = await aiohttp_client(_app(tmp_path))
    resp = await client.put("/soul", json={"content": "x"})
    assert resp.status == 401
    assert not (tmp_path / "SOUL.md").exists()


async def test_put_soul_rejects_wrong_token(aiohttp_client, tmp_path):
    client = await aiohttp_client(_app(tmp_path))
    resp = await client.put(
        "/soul", json={"content": "x"}, headers={"Authorization": "Bearer nope"}
    )
    assert resp.status == 401


async def test_put_soul_empty_rejected(aiohttp_client, tmp_path):
    client = await aiohttp_client(_app(tmp_path))
    resp = await client.put("/soul", json={"content": "   "}, headers=AUTH)
    assert resp.status == 400


async def test_get_soul_reads_with_auth(aiohttp_client, tmp_path):
    (tmp_path / "SOUL.md").write_text("# Sol\nhi", encoding="utf-8")
    client = await aiohttp_client(_app(tmp_path))
    resp = await client.get("/soul", headers=AUTH)
    body = await resp.json()
    assert body == {"ok": True, "content": "# Sol\nhi"}

    resp = await client.get("/soul")  # no auth
    assert resp.status == 401


# --- model config parsing -------------------------------------------------


def test_find_model_value():
    assert _find_model_value(SAMPLE_CONFIG) == "gemma4:e4b"
    assert _find_model_value("memory:\n  provider: x\n") == ""


def test_set_model_in_config_replaces_only_model_model():
    out = _set_model_in_config(SAMPLE_CONFIG, "llama3:8b")
    assert "  model: llama3:8b\n" in out
    assert "gemma4:e4b" not in out
    # Everything else is preserved.
    assert "  provider: custom\n" in out
    assert "  base_url: http://127.0.0.1:11434/v1\n" in out
    assert "  provider: holographic\n" in out
    assert "servicebay-mcp:" in out


def test_set_model_in_config_none_when_no_field():
    assert _set_model_in_config("memory:\n  provider: x\n", "llama3:8b") is None


def test_servicebay_mcp_creds():
    url, token = _servicebay_mcp_creds(SAMPLE_CONFIG)
    assert url == "http://127.0.0.1:5888/mcp"
    assert token == "sb_0a1b2c3d_ABCDEF234567"
    assert _servicebay_mcp_creds("model:\n  model: x\n") == ("", "")


# --- model endpoints ------------------------------------------------------


def _model_app(tmp_path, cfg=SAMPLE_CONFIG):
    cfgp = tmp_path / "config.yaml"
    cfgp.write_text(cfg, encoding="utf-8")
    return (
        build_app(
            token=TOKEN,
            soul_path=str(tmp_path / "SOUL.md"),
            config_path=str(cfgp),
        ),
        cfgp,
    )


async def test_get_model(aiohttp_client, tmp_path, monkeypatch):
    async def fake_tags(url):
        return ["gemma4:e4b", "llama3:8b"]

    monkeypatch.setattr(config_agent, "_ollama_tags", fake_tags)
    app, _ = _model_app(tmp_path)
    client = await aiohttp_client(app)

    resp = await client.get("/model", headers=AUTH)
    body = await resp.json()
    assert resp.status == 200
    assert body["current"] == "gemma4:e4b"
    assert body["available"] == ["gemma4:e4b", "llama3:8b"]

    assert (await client.get("/model")).status == 401  # no auth


async def test_put_model_writes_and_signals_restart(
    aiohttp_client, tmp_path, monkeypatch
):
    restarts = []

    async def fake_restart(url, token, service):
        restarts.append((url, token, service))
        return True

    monkeypatch.setattr(config_agent, "_restart_via_sbmcp", fake_restart)
    app, cfgp = _model_app(tmp_path)
    client = await aiohttp_client(app)

    resp = await client.put("/model", json={"model": "llama3:8b"}, headers=AUTH)
    body = await resp.json()
    assert resp.status == 200
    assert body == {"ok": True, "restarted": True}
    assert "  model: llama3:8b\n" in cfgp.read_text(encoding="utf-8")
    # config.yaml holds secrets — must stay 0600.
    assert (os.stat(cfgp).st_mode & 0o777) == 0o600


async def test_put_model_no_restart_creds(aiohttp_client, tmp_path):
    # config without an mcp servicebay token → write but no restart.
    cfg = "model:\n  provider: custom\n  model: gemma4:e4b\n"
    app, cfgp = _model_app(tmp_path, cfg=cfg)
    client = await aiohttp_client(app)
    resp = await client.put("/model", json={"model": "llama3:8b"}, headers=AUTH)
    body = await resp.json()
    assert body["ok"] is True and body["restarted"] is False
    assert "  model: llama3:8b\n" in cfgp.read_text(encoding="utf-8")


async def test_put_model_auth_and_empty(aiohttp_client, tmp_path):
    app, _ = _model_app(tmp_path)
    client = await aiohttp_client(app)
    assert (await client.put("/model", json={"model": "x"})).status == 401
    assert (
        await client.put("/model", json={"model": "  "}, headers=AUTH)
    ).status == 400


# --- MCP servers (Tools tab) ----------------------------------------------


def test_parse_mcp_servers():
    servers = _parse_mcp_servers(SAMPLE_CONFIG)
    by = {s["name"]: s for s in servers}
    assert set(by) == {"ha-mcp", "servicebay-mcp"}
    assert by["servicebay-mcp"]["url"] == "http://127.0.0.1:5888/mcp"
    assert by["servicebay-mcp"]["token"] == "sb_0a1b2c3d_ABCDEF234567"
    assert _parse_mcp_servers("model:\n  model: x\n") == []


async def test_get_mcp_lists_servers_without_tokens(
    aiohttp_client, tmp_path, monkeypatch
):
    async def fake_probe(url, token):
        return {"reachable": True, "tools": ["tool_a", "tool_b"], "error": ""}

    monkeypatch.setattr(config_agent, "_mcp_probe", fake_probe)
    app, _ = _model_app(tmp_path)  # SAMPLE_CONFIG has ha-mcp + servicebay-mcp
    client = await aiohttp_client(app)

    resp = await client.get("/mcp", headers=AUTH)
    body = await resp.json()
    assert resp.status == 200
    names = {s["name"] for s in body["servers"]}
    assert names == {"ha-mcp", "servicebay-mcp"}
    for s in body["servers"]:
        assert s["reachable"] is True and s["tools"] == ["tool_a", "tool_b"]
        assert s["error"] == ""
        assert "token" not in s  # tokens never leave the agent

    assert (await client.get("/mcp")).status == 401  # no auth


# --- Connectivity diagnostics + interactive tester (#191) -----------------


def test_diagnose_connect_error_categories():
    assert "Timeout" in _diagnose_connect_error(TimeoutError())

    class _FakeConnectorError(aiohttp.ClientConnectorError):
        def __init__(self):
            Exception.__init__(self, "no route")

        def __str__(self):
            return "no route"

    assert "Network/DNS" in _diagnose_connect_error(_FakeConnectorError())
    assert "Connection error" in _diagnose_connect_error(aiohttp.ClientError("boom"))


async def test_get_mcp_surfaces_error_when_unreachable(
    aiohttp_client, tmp_path, monkeypatch
):
    async def fake_probe(url, token):
        return {
            "reachable": False,
            "tools": [],
            "error": "Authentication failed — server returned 401",
        }

    monkeypatch.setattr(config_agent, "_mcp_probe", fake_probe)
    app, _ = _model_app(tmp_path)
    client = await aiohttp_client(app)
    body = await (await client.get("/mcp", headers=AUTH)).json()
    for s in body["servers"]:
        assert s["reachable"] is False
        assert "Authentication failed" in s["error"]


async def test_test_mcp_invokes_named_server_tool(
    aiohttp_client, tmp_path, monkeypatch
):
    calls = []

    async def fake_call(url, token, tool, arguments):
        calls.append((url, token, tool, arguments))
        return {"ok": True, "result": {"echo": arguments}}

    monkeypatch.setattr(config_agent, "_mcp_call_tool", fake_call)
    app, _ = _model_app(tmp_path)
    client = await aiohttp_client(app)

    resp = await client.post(
        "/mcp/servicebay-mcp/test",
        json={"tool": "restart_service", "arguments": {"name": "hermes"}},
        headers=AUTH,
    )
    body = await resp.json()
    assert resp.status == 200
    assert body == {"ok": True, "result": {"echo": {"name": "hermes"}}}
    # The agent resolves the server's url+token from config — caller never sees it.
    assert calls == [
        (
            "http://127.0.0.1:5888/mcp",
            "sb_0a1b2c3d_ABCDEF234567",
            "restart_service",
            {"name": "hermes"},
        )
    ]


async def test_test_mcp_unknown_server_404(aiohttp_client, tmp_path):
    app, _ = _model_app(tmp_path)
    client = await aiohttp_client(app)
    resp = await client.post("/mcp/nope-mcp/test", json={"tool": "x"}, headers=AUTH)
    assert resp.status == 404


async def test_test_mcp_auth_and_validation(aiohttp_client, tmp_path):
    app, _ = _model_app(tmp_path)
    client = await aiohttp_client(app)
    assert (
        await client.post("/mcp/servicebay-mcp/test", json={"tool": "x"})
    ).status == 401
    assert (
        await client.post("/mcp/servicebay-mcp/test", json={"tool": "  "}, headers=AUTH)
    ).status == 400
    assert (
        await client.post(
            "/mcp/servicebay-mcp/test",
            json={"tool": "x", "arguments": "nope"},
            headers=AUTH,
        )
    ).status == 400

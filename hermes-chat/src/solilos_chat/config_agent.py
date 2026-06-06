"""Privileged config sidecar — runs *inside* the hermes pod.

The public chat pod cannot write Hermes' identity/config: SOUL.md and
config.yaml live in Hermes' own data dir (`/opt/data`, mode 0700, owned by
a foreign subuid), unreachable from the rootless chat pod's mount. This tiny
service runs as a second container in the hermes pod, mounts that data dir
read-write, and exposes a narrow loopback API the chat pod calls to apply an
admin's soul edit and model switch.

It is **not** internet-facing — it binds loopback only, behind no proxy, and
authenticates with the *same* Hermes API key the chat pod already holds
(`API_SERVER_KEY`), so no new secret crosses the wire. The admin gate stays
in the chat pod (Authelia `Remote-Groups`); this agent only trusts that a
caller proving the Hermes key is an internal Solilos service.

SOUL.md is loaded fresh by Hermes on every message, so a soul write takes
effect live — no restart. A model change rewrites config.yaml and restarts
Hermes, reusing the SB-MCP token already in config.yaml (lifecycle scope) —
again, no new credential.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
from pathlib import Path

import aiohttp
from aiohttp import web

from solilos_chat.logging import log


def _atomic_write(path: str, content: str, mode: int = 0o644) -> None:
    """Write `content` to `path` atomically (temp in the same dir + replace).

    Default 0644 (SOUL.md — readable, not secret). config.yaml is written
    0600: it holds the Hermes API key and the SB-MCP token, so it must not
    widen beyond the hermes user."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.chmod(tmp, mode)
        os.replace(tmp, target)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


_MODEL_CHILD = re.compile(r"^ {2}model:\s*(.*)$")


def _find_model_value(text: str) -> str:
    """Read `model.model` (the top-level `model:` block's `model:` child)."""
    in_block = False
    for line in text.splitlines():
        if line[:1] not in (" ", "\t"):
            in_block = line.strip() == "model:"
            continue
        if in_block:
            m = _MODEL_CHILD.match(line)
            if m:
                return m.group(1).strip().strip("\"'")
    return ""


def _set_model_in_config(text: str, model: str) -> str | None:
    """Return config text with `model.model` set to `model`, or None when
    there is no such line (don't guess where to insert it)."""
    lines = text.splitlines(keepends=True)
    in_block = False
    for i, line in enumerate(lines):
        body = line.rstrip("\n")
        if body[:1] not in (" ", "\t"):
            in_block = body.strip() == "model:"
            continue
        if in_block and _MODEL_CHILD.match(body):
            nl = "\n" if line.endswith("\n") else ""
            lines[i] = f"  model: {model}{nl}"
            return "".join(lines)
    return None


def _servicebay_mcp_creds(text: str) -> tuple[str, str]:
    """Pull (url, bearer) for the `mcp_servers.servicebay-mcp` entry from
    config.yaml — the SB-MCP token Hermes already holds (read+lifecycle
    scope), reused to restart Hermes. ('', '') when absent."""
    in_block = in_sb = False
    url = token = ""
    for line in text.splitlines():
        if line[:1] not in (" ", "\t"):
            in_block = line.strip() == "mcp_servers:"
            in_sb = False
            continue
        if not in_block:
            continue
        if re.match(r"^ {2}\S", line) and line.strip().endswith(":"):
            in_sb = line.strip() == "servicebay-mcp:"
            continue
        if in_sb:
            u = re.match(r'^\s*url:\s*"?([^"\s]+)"?', line)
            if u:
                url = u.group(1)
            a = re.search(r"Bearer\s+([^\"\s]+)", line)
            if a:
                token = a.group(1)
    return url, token


def _parse_mcp_servers(text: str) -> list[dict[str, str]]:
    """All `mcp_servers:` entries from config.yaml as [{name,url,token}]."""
    servers: list[dict[str, str]] = []
    in_block = False
    cur: dict[str, str] | None = None
    for line in text.splitlines():
        if line[:1] not in (" ", "\t"):
            if cur:
                servers.append(cur)
                cur = None
            in_block = line.strip() == "mcp_servers:"
            continue
        if not in_block:
            continue
        if re.match(r"^ {2}\S", line) and line.strip().endswith(":"):
            if cur:
                servers.append(cur)
            cur = {"name": line.strip()[:-1], "url": "", "token": ""}
            continue
        if cur is not None:
            u = re.match(r'^\s*url:\s*"?([^"\s]+)"?', line)
            if u:
                cur["url"] = u.group(1)
            a = re.search(r"Bearer\s+([^\"\s]+)", line)
            if a:
                cur["token"] = a.group(1)
    if cur:
        servers.append(cur)
    return servers


async def _mcp_list_tools(url: str, token: str) -> tuple[bool, list[str]]:
    """Probe an MCP server: returns (reachable, [tool names]). A streamable
    HTTP MCP replies either as JSON or SSE-framed `data:` lines."""
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Authorization": f"Bearer {token}",
        "Origin": url,
    }

    async def rpc(session, sid, method, params=None, notif=False):
        body = {"jsonrpc": "2.0", "method": method}
        if not notif:
            body["id"] = 1
        if params is not None:
            body["params"] = params
        h = dict(headers)
        if sid:
            h["mcp-session-id"] = sid
        async with session.post(url, json=body, headers=h) as r:
            return r.status, r.headers.get("mcp-session-id"), await r.text()

    def parse(txt: str) -> dict:
        if "\ndata:" in txt or txt.startswith("data:"):
            txt = "\n".join(
                ln[5:].strip() for ln in txt.splitlines() if ln.startswith("data:")
            )
        try:
            return json.loads(txt)
        except (ValueError, TypeError):
            return {}

    try:
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            st, sid, _ = await rpc(
                s,
                None,
                "initialize",
                {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "config-agent", "version": "1"},
                },
            )
            if st >= 400:
                return False, []
            await rpc(s, sid, "notifications/initialized", notif=True)
            st, _, txt = await rpc(s, sid, "tools/list", {})
            if st >= 400:
                return True, []
            data = parse(txt)
            tools = (
                data.get("result", {}).get("tools", [])
                if isinstance(data, dict)
                else []
            )
            names = [t.get("name") for t in tools if isinstance(t, dict)]
            return True, sorted(n for n in names if isinstance(n, str) and n)
    except (aiohttp.ClientError, TimeoutError, OSError) as e:
        log.error("agent.mcp.probe_failed", url=url, error=str(e))
        return False, []


async def _ollama_tags(ollama_url: str) -> list[str]:
    """Installed Ollama model tags (the switch's options); [] on failure."""
    base = ollama_url.rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    try:
        timeout = aiohttp.ClientTimeout(total=8)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.get(f"{base}/api/tags") as r:
                if r.status >= 400:
                    return []
                data = await r.json()
    except (aiohttp.ClientError, TimeoutError, OSError, ValueError):
        return []
    tags = [m.get("name") for m in data.get("models", []) if isinstance(m, dict)]
    return sorted(t for t in tags if isinstance(t, str) and t)


async def _restart_via_sbmcp(url: str, token: str, service: str) -> bool:
    """Restart `service` through ServiceBay-MCP using Hermes' own SB-MCP
    token (lifecycle scope). Returns True when the call was accepted."""
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Authorization": f"Bearer {token}",
        "Origin": url,
    }

    async def rpc(session, sid, method, params=None, notif=False):
        body = {"jsonrpc": "2.0", "method": method}
        if not notif:
            body["id"] = 1
        if params is not None:
            body["params"] = params
        h = dict(headers)
        if sid:
            h["mcp-session-id"] = sid
        async with session.post(url, json=body, headers=h) as r:
            return r.status, r.headers.get("mcp-session-id")

    try:
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            status, sid = await rpc(
                s,
                None,
                "initialize",
                {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "config-agent", "version": "1"},
                },
            )
            if status >= 400:
                log.error("agent.restart.init_failed", status=status)
                return False
            await rpc(s, sid, "notifications/initialized", notif=True)
            status, _ = await rpc(
                s,
                sid,
                "tools/call",
                {"name": "restart_service", "arguments": {"name": service}},
            )
            return status < 400
    except (aiohttp.ClientError, TimeoutError, OSError) as e:
        log.error("agent.restart.unreachable", error=str(e))
        return False


def build_app(
    *,
    token: str,
    soul_path: str,
    config_path: str = "/opt/data/config.yaml",
    ollama_url: str = "http://127.0.0.1:11434",
    hermes_service: str = "hermes",
) -> web.Application:
    def authorized(request: web.Request) -> bool:
        # Constant work regardless of presence; the key is a shared secret,
        # not a per-user credential, so a plain compare is fine here.
        auth = request.headers.get("Authorization", "")
        return bool(token) and auth == f"Bearer {token}"

    async def health(_request: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    async def get_soul(request: web.Request) -> web.Response:
        if not authorized(request):
            return web.json_response(
                {"ok": False, "reason": "unauthorized"}, status=401
            )
        try:
            content = Path(soul_path).read_text(encoding="utf-8")
        except OSError:
            content = ""
        return web.json_response({"ok": True, "content": content})

    async def put_soul(request: web.Request) -> web.Response:
        if not authorized(request):
            return web.json_response(
                {"ok": False, "reason": "unauthorized"}, status=401
            )
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 — any malformed JSON
            return web.json_response(
                {"ok": False, "reason": "invalid_json"}, status=400
            )
        content = body.get("content")
        if not isinstance(content, str) or not content.strip():
            return web.json_response(
                {"ok": False, "reason": "empty_content"}, status=400
            )
        try:
            _atomic_write(soul_path, content)
        except OSError as e:
            log.error("agent.soul.write_failed", path=soul_path, error=str(e))
            return web.json_response(
                {"ok": False, "reason": "write_failed"}, status=500
            )
        log.info("agent.soul.written", path=soul_path)
        return web.json_response({"ok": True})

    async def get_model(request: web.Request) -> web.Response:
        if not authorized(request):
            return web.json_response(
                {"ok": False, "reason": "unauthorized"}, status=401
            )
        try:
            text = Path(config_path).read_text(encoding="utf-8")
        except OSError:
            text = ""
        return web.json_response(
            {
                "ok": True,
                "current": _find_model_value(text),
                "available": await _ollama_tags(ollama_url),
            }
        )

    async def put_model(request: web.Request) -> web.Response:
        if not authorized(request):
            return web.json_response(
                {"ok": False, "reason": "unauthorized"}, status=401
            )
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 — any malformed JSON
            return web.json_response(
                {"ok": False, "reason": "invalid_json"}, status=400
            )
        model = body.get("model")
        if not isinstance(model, str) or not model.strip():
            return web.json_response({"ok": False, "reason": "empty_model"}, status=400)
        model = model.strip()
        try:
            text = Path(config_path).read_text(encoding="utf-8")
        except OSError:
            return web.json_response(
                {"ok": False, "reason": "config_unavailable"}, status=500
            )
        new = _set_model_in_config(text, model)
        if new is None:
            return web.json_response(
                {"ok": False, "reason": "no_model_field"}, status=500
            )
        if new == text:
            return web.json_response({"ok": True, "restarted": False})  # no change
        try:
            _atomic_write(config_path, new, mode=0o600)
        except OSError as e:
            log.error("agent.model.write_failed", path=config_path, error=str(e))
            return web.json_response(
                {"ok": False, "reason": "write_failed"}, status=500
            )
        url, mcp_token = _servicebay_mcp_creds(text)
        if not (url and mcp_token):
            log.warn("agent.model.no_restart_creds")
            return web.json_response(
                {"ok": True, "restarted": False, "reason": "no_restart_creds"}
            )
        # Restart AFTER the response is sent — restarting the hermes pod
        # kills this sidecar, so we must not await it before replying.
        asyncio.create_task(_restart_via_sbmcp(url, mcp_token, hermes_service))
        log.info("agent.model.set", model=model)
        return web.json_response({"ok": True, "restarted": True})

    async def get_mcp(request: web.Request) -> web.Response:
        # The MCP servers Hermes is wired to (config.yaml mcp_servers) — name,
        # url, live reachability and the tools each exposes. Tokens are NEVER
        # returned. These are absent from Hermes' /v1/toolsets, so the panel
        # surfaces them here.
        if not authorized(request):
            return web.json_response(
                {"ok": False, "reason": "unauthorized"}, status=401
            )
        try:
            text = Path(config_path).read_text(encoding="utf-8")
        except OSError:
            text = ""
        out = []
        for s in _parse_mcp_servers(text):
            reachable, tools = (
                await _mcp_list_tools(s["url"], s["token"]) if s["url"] else (False, [])
            )
            out.append(
                {
                    "name": s["name"],
                    "url": s["url"],
                    "reachable": reachable,
                    "tools": tools,
                }
            )
        return web.json_response({"ok": True, "servers": out})

    app = web.Application()
    app.router.add_get("/health", health)
    app.router.add_get("/soul", get_soul)
    app.router.add_put("/soul", put_soul)
    app.router.add_get("/model", get_model)
    app.router.add_put("/model", put_model)
    app.router.add_get("/mcp", get_mcp)
    return app


async def serve(
    host: str,
    port: int,
    *,
    token: str,
    soul_path: str,
    config_path: str,
    ollama_url: str,
    hermes_service: str,
) -> None:
    app = build_app(
        token=token,
        soul_path=soul_path,
        config_path=config_path,
        ollama_url=ollama_url,
        hermes_service=hermes_service,
    )
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    log.info("agent.listening", host=host, port=port, soul_path=soul_path)
    await asyncio.Event().wait()


def main() -> None:
    host = os.environ.get("AGENT_HOST", "127.0.0.1")
    port = int(os.environ.get("AGENT_PORT", "8650"))
    # Reuse the Hermes API key as the shared internal secret (no new secret).
    token = os.environ.get("API_SERVER_KEY", "")
    soul_path = os.environ.get("SOUL_PATH", "/opt/data/SOUL.md")
    config_path = os.environ.get("CONFIG_PATH", "/opt/data/config.yaml")
    ollama_url = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
    hermes_service = os.environ.get("HERMES_SERVICE", "hermes")
    log.info("agent.boot", host=host, port=port)
    asyncio.run(
        serve(
            host,
            port,
            token=token,
            soul_path=soul_path,
            config_path=config_path,
            ollama_url=ollama_url,
            hermes_service=hermes_service,
        )
    )


if __name__ == "__main__":
    main()

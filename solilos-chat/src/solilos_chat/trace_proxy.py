"""Always-on Ollama trace proxy — permanent LLM traceability.

Sits PERMANENTLY between the Hermes gateway and Ollama: Hermes'
`providers.ollama.api` points here, we transparently forward to the real Ollama
and capture a structured trace of every LLM call — the prompt block sizes
(system / tools / history / user), the full tool list with per-tool sizes, the
token usage, and the wall time — into a ring buffer served at `GET /__traces__`.
The chat client reads it to enrich each turn's trace, so we can SEE what is
actually dragged into the prompt (which skills, which MCP tool descriptions) and
where the tokens/time go.

FAIL-OPEN by construction: forwarding happens first and a capture/parse error
NEVER affects the response — the LLM path keeps working even if tracing hiccups.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections import deque
from typing import Any

import aiohttp
from aiohttp import web

from solilos_chat.logging import log

UPSTREAM = os.environ.get("OLLAMA_UPSTREAM", "http://127.0.0.1:11434").rstrip("/")
HOST = os.environ.get("TRACE_PROXY_HOST", "127.0.0.1")
PORT = int(os.environ.get("TRACE_PROXY_PORT", "11436"))
RING = int(os.environ.get("TRACE_RING", "200"))
# The LLM-call paths we summarise; everything else (GET /api/tags, /api/show,
# embeddings, …) is forwarded transparently but not traced.
CAPTURE_PATHS = ("/v1/chat/completions", "/api/chat")

_traces: deque[dict[str, Any]] = deque(maxlen=RING)


def _toks(chars: int) -> int:
    """Rough token estimate from a character count (~4 chars/token). Used only
    for the per-block *split*; the per-call total comes from Ollama's `usage`."""
    return round(chars / 4)


def summarize_request(body: bytes) -> dict[str, Any]:
    """Break a /v1/chat/completions (or /api/chat) request into its blocks:
    per-role message char sizes and the tool list with per-tool sizes."""
    d = json.loads(body)
    msgs = d.get("messages", []) or []
    tools = d.get("tools", []) or []
    blocks: dict[str, int] = {}
    for m in msgs:
        c = m.get("content")
        cl = len(c) if isinstance(c, str) else (len(json.dumps(c)) if c else 0)
        blocks[m.get("role", "?")] = blocks.get(m.get("role", "?"), 0) + cl
    tool_list = []
    tools_chars = 0
    for t in tools:
        tj = json.dumps(t)
        tools_chars += len(tj)
        tool_list.append(
            {
                "name": (t.get("function") or {}).get("name"),
                "chars": len(tj),
                "tok_est": _toks(len(tj)),
            }
        )
    return {
        "model": d.get("model"),
        "stream": bool(d.get("stream")),
        "num_ctx": (d.get("options") or {}).get("num_ctx"),
        "blocks_chars": blocks,
        "n_tools": len(tools),
        "tools_chars": tools_chars,
        "tools": tool_list,
    }


def summarize_response(raw: bytes) -> dict[str, Any]:
    """Pull `usage`, `finish_reason` and any tool-call names out of a response,
    handling both a single JSON body and an SSE stream (last usage wins)."""
    text = raw.decode("utf-8", "replace")
    usage = None
    finish = None
    tool_calls: list[str] = []
    if text.lstrip().startswith("{"):
        try:
            d = json.loads(text)
            usage = d.get("usage")
            ch = (d.get("choices") or [{}])[0]
            finish = ch.get("finish_reason")
            tc = (ch.get("message") or {}).get("tool_calls") or []
            tool_calls = [(t.get("function") or {}).get("name") for t in tc]
        except (ValueError, TypeError):
            pass
    else:
        for line in text.splitlines():
            if line.startswith("data:") and '"usage"' in line:
                try:
                    u = json.loads(line[5:].strip()).get("usage")
                    if u:
                        usage = u
                except ValueError:
                    pass
        fm = re.findall(r'"finish_reason":\s*"(\w+)"', text)
        if fm:
            finish = fm[-1]
        tool_calls = list(dict.fromkeys(re.findall(r'"name":"(\w+)"', text)))
    return {"usage": usage, "finish_reason": finish, "tool_calls": tool_calls}


def build_record(
    path: str, req: dict[str, Any], resp: dict[str, Any], wall: float
) -> dict[str, Any]:
    """Fold a request+response summary into one trace record, splitting the
    ground-truth `prompt_tokens` across the blocks proportionally to their
    character sizes so the per-block token figures sum to the real total."""
    usage = resp.get("usage") or {}
    prompt_tokens = usage.get("prompt_tokens")
    blocks_chars = req.get("blocks_chars", {})
    tools_chars = req.get("tools_chars", 0)
    total_chars = sum(blocks_chars.values()) + tools_chars
    blocks_tok: dict[str, int] = {}
    tools_tok = 0
    if prompt_tokens and total_chars:
        scale = prompt_tokens / total_chars
        for role, ch in blocks_chars.items():
            blocks_tok[role] = round(ch * scale)
        tools_tok = round(tools_chars * scale)
    else:  # no usage yet — fall back to the char estimate
        blocks_tok = {r: _toks(ch) for r, ch in blocks_chars.items()}
        tools_tok = _toks(tools_chars)
    num_ctx = req.get("num_ctx")
    return {
        "ts": time.time(),
        "wall_s": round(wall, 3),
        "path": path,
        "model": req.get("model"),
        "stream": req.get("stream"),
        "num_ctx": num_ctx,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": usage.get("completion_tokens"),
        "total_tokens": usage.get("total_tokens"),
        "context_free": (num_ctx - prompt_tokens)
        if (num_ctx and prompt_tokens)
        else None,
        "blocks_tok": blocks_tok,
        "tools_tok": tools_tok,
        "n_tools": req.get("n_tools"),
        "tools": req.get("tools"),
        "finish_reason": resp.get("finish_reason"),
        "tool_calls": resp.get("tool_calls"),
    }


async def handle(request: web.Request) -> web.StreamResponse:
    path = request.rel_url.path
    if path.startswith("/__traces__"):
        if path.endswith("/health"):
            return web.json_response({"ok": True, "count": len(_traces)})
        return web.json_response(list(_traces)[::-1])  # newest first

    body = await request.read()
    t0 = time.time()
    url = UPSTREAM + str(request.rel_url)
    captured = bytearray()
    fwd_headers = {k: v for k, v in request.headers.items() if k.lower() != "host"}
    resp: web.StreamResponse | None = None
    try:
        async with aiohttp.ClientSession(auto_decompress=False) as sess:
            async with sess.request(
                request.method,
                url,
                data=body,
                headers=fwd_headers,
                timeout=aiohttp.ClientTimeout(total=600),
            ) as up:
                resp = web.StreamResponse(status=up.status)
                for k, v in up.headers.items():
                    if k.lower() in (
                        "transfer-encoding",
                        "content-length",
                        "content-encoding",
                        "connection",
                    ):
                        continue
                    resp.headers[k] = v
                await resp.prepare(request)
                async for chunk in up.content.iter_chunked(8192):
                    captured.extend(chunk)
                    await resp.write(chunk)
                await resp.write_eof()
    except Exception as e:  # noqa: BLE001 — fail-open: forwarding failure only
        log.error("trace.forward_error", path=path, error=str(e))
        if resp is None:
            return web.Response(status=502, text="trace proxy: upstream error")
        return resp

    if any(p in path for p in CAPTURE_PATHS):
        try:
            req_sum = summarize_request(body)
            resp_sum = summarize_response(bytes(captured))
            _traces.append(build_record(path, req_sum, resp_sum, time.time() - t0))
        except Exception as e:  # noqa: BLE001 — capture must never break the path
            log.warn("trace.capture_error", path=path, error=str(e))
    return resp


def main() -> None:
    log.info("trace.boot", host=HOST, port=PORT, upstream=UPSTREAM, ring=RING)
    app = web.Application(client_max_size=256 * 1024 * 1024)
    app.router.add_route("*", "/{tail:.*}", handle)
    web.run_app(app, host=HOST, port=PORT, print=None)


if __name__ == "__main__":
    main()

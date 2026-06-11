"""Thin streaming client for Ollama's native `/api/chat`.

The native endpoint (not `/v1/chat/completions`) is deliberate: it honors
`think` for reasoning control and streams `message.thinking` separately from
`message.content`, so the engine never parses `<thinking>` tags out of the
answer. No `num_ctx` is sent — the box-wide OLLAMA_CONTEXT_LENGTH rules, and
a per-request value would reload the model with a different KV size.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

import aiohttp


class OllamaError(Exception):
    """Raised when Ollama returns a non-2xx response."""


@dataclass
class ChatResult:
    """One completed `/api/chat` call, deltas folded together."""

    content: str = ""
    thinking: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    wall_s: float = 0.0
    ttft_s: float = 0.0


class OllamaChat:
    def __init__(self, base_url: str, timeout: float = 300.0):
        self._base_url = base_url.rstrip("/")
        self._timeout = aiohttp.ClientTimeout(total=timeout, sock_read=timeout)

    async def stream(
        self,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        think: bool = False,
    ):
        """Yield `("delta", str)` / `("thinking", str)` per chunk, then one
        final `("done", ChatResult)`. Closing the generator aborts the HTTP
        request — that is what actually interrupts the model's generation.
        """
        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": True,
            "think": think,
        }
        if tools:
            body["tools"] = tools
        result = ChatResult()
        t0 = time.monotonic()
        async with aiohttp.ClientSession(timeout=self._timeout) as client:
            async with client.post(f"{self._base_url}/api/chat", json=body) as resp:
                if resp.status >= 400:
                    detail = (await resp.text())[:500]
                    raise OllamaError(f"ollama /api/chat {resp.status}: {detail}")
                async for raw in resp.content:
                    line = raw.strip()
                    if not line:
                        continue
                    chunk = json.loads(line)
                    msg = chunk.get("message") or {}
                    delta = msg.get("content") or ""
                    thinking = msg.get("thinking") or ""
                    if (
                        delta or thinking or msg.get("tool_calls")
                    ) and not result.ttft_s:
                        result.ttft_s = time.monotonic() - t0
                    if delta:
                        result.content += delta
                        yield "delta", delta
                    if thinking:
                        result.thinking += thinking
                        yield "thinking", thinking
                    for tc in msg.get("tool_calls") or []:
                        result.tool_calls.append(tc)
                    if chunk.get("done"):
                        result.prompt_tokens = chunk.get("prompt_eval_count") or 0
                        result.completion_tokens = chunk.get("eval_count") or 0
        result.wall_s = time.monotonic() - t0
        yield "done", result

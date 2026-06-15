"""Native LLM tracing — full parity with the retired trace proxy.

The engine records every Ollama call at the call site into the same
light-ring + detail-ring shapes the proxy served, so the trace panel, the
per-turn waterfall and the persisted `session_traces` rows keep working
unchanged. Improvement over the proxy: records carry the `session_id`
directly (the engine knows it), so the wall-clock-window correlation hack
disappears — a turn's steps are an exact filter, not a time guess.
"""

from __future__ import annotations

import time
from collections import deque
from typing import Any

RING = 200
DETAIL_RING = 30


def _toks(chars: int) -> int:
    """Rough token estimate (~4 chars/token), only for the per-block split;
    totals come from Ollama's ground-truth counts."""
    return round(chars / 4)


class TraceRecorder:
    def __init__(self, ring: int = RING, detail_ring: int = DETAIL_RING):
        self._traces: deque[dict[str, Any]] = deque(maxlen=ring)
        self._details: dict[int, dict[str, Any]] = {}
        self._detail_ring = detail_ring
        self._next_id = 0

    def record(
        self,
        *,
        session_id: str,
        profile: str,
        model: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        content: str,
        thinking: str,
        tool_calls: list[dict[str, Any]],
        prompt_tokens: int,
        completion_tokens: int,
        wall_s: float,
        context_window: int | None,
    ) -> dict[str, Any]:
        """Append one LLM call; returns the light record (with its id)."""
        import json

        blocks_chars: dict[str, int] = {}
        for m in messages:
            c = m.get("content")
            cl = len(c) if isinstance(c, str) else (len(json.dumps(c)) if c else 0)
            role = m.get("role", "?")
            blocks_chars[role] = blocks_chars.get(role, 0) + cl
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
        total_chars = sum(blocks_chars.values()) + tools_chars
        blocks_tok: dict[str, int] = {}
        tools_tok = 0
        if prompt_tokens and total_chars:
            scale = prompt_tokens / total_chars
            blocks_tok = {r: round(ch * scale) for r, ch in blocks_chars.items()}
            tools_tok = round(tools_chars * scale)
        else:
            blocks_tok = {r: _toks(ch) for r, ch in blocks_chars.items()}
            tools_tok = _toks(tools_chars)
        record = {
            "ts": time.time(),
            "wall_s": round(wall_s, 3),
            "step_kind": "llm",
            "path": "/api/chat",
            "session_id": session_id,
            "model": model,
            "profile": profile,
            "stream": True,
            "num_ctx": context_window,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
            "context_free": (context_window - prompt_tokens)
            if (context_window and prompt_tokens)
            else None,
            "blocks_tok": blocks_tok,
            "tools_tok": tools_tok,
            "n_tools": len(tools),
            "tools": tool_list,
            "finish_reason": "tool_calls" if tool_calls else "stop",
            "tool_calls": [(tc.get("function") or {}).get("name") for tc in tool_calls],
        }
        detail = {
            "path": "/api/chat",
            "request": {"model": model, "tools": tools, "messages": messages},
            "response": {
                "final": content,
                "thinking": thinking,
                "tool_calls": tool_calls,
            },
        }
        rec_id = self._next_id
        self._next_id += 1
        record["id"] = rec_id
        self._traces.append(record)
        self._details[rec_id] = detail
        while len(self._details) > self._detail_ring:
            del self._details[next(iter(self._details))]
        return record

    def record_tool(
        self,
        *,
        session_id: str,
        profile: str,
        tool_name: str,
        wall_s: float,
    ) -> dict[str, Any]:
        """Append one tool-execution step, interleaved by append order with the
        LLM steps so a turn's `for_session` reads back the exact run sequence."""
        record = {
            "ts": time.time(),
            "wall_s": round(wall_s, 3),
            "step_kind": "tool",
            "session_id": session_id,
            "profile": profile,
            "tool_name": tool_name,
        }
        rec_id = self._next_id
        self._next_id += 1
        record["id"] = rec_id
        self._traces.append(record)
        return record

    def for_session(self, session_id: str, since_ts: float) -> list[dict[str, Any]]:
        """A turn's steps, oldest first — exact session filter, no time guess."""
        return [
            r
            for r in self._traces
            if r.get("session_id") == session_id and r["ts"] >= since_ts
        ]

    def list_traces(self) -> list[dict[str, Any]]:
        return list(self._traces)[::-1]  # newest first, like the proxy

    def detail(self, rec_id: int) -> dict[str, Any] | None:
        return self._details.get(rec_id)

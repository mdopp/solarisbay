"""EngineClient — the in-process replacement for a Hermes gateway.

Implements the HermesClient surface (create/list/get/delete session,
set_title, chat, chat_stream, list_toolsets) so `server.py`'s routing,
compaction and the browser SSE protocol keep working unchanged — but the
"gateway" is a profile object: a model tag, a soul, a toolbox and an optional
entity registry, all sharing one store, one Ollama connection and one trace
recorder. Three of these replace the three Hermes gateways; what used to be
a container-and-port is now a constructor call.

Events yielded by `chat_stream` mirror the Hermes SSE shapes `_normalize`
folds for the browser: `assistant.delta`, `tool.started`/`tool.completed`,
`run.completed` (with `reasoning_content` on the final assistant message).
"""

from __future__ import annotations

import contextvars
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from solilos_chat.engine import store
from solilos_chat.engine.ollama import OllamaChat, OllamaError
from solilos_chat.engine.registry import EntityRegistry
from solilos_chat.engine.tools import Toolbox
from solilos_chat.engine.trace import TraceRecorder
from solilos_chat.logging import log

# The current turn's resident — read by tools (timers, facts) that need an
# owner. A contextvar because the toolbox is built once per profile but a
# turn belongs to whoever sent it.
current_uid: contextvars.ContextVar[str] = contextvars.ContextVar(
    "engine_uid", default=""
)

# Tool-call passes per turn: enough for list->act->confirm chains plus a
# retry, small enough that a confused model can't spin.
_MAX_PASSES = 6

_LOCAL_TZ = ZoneInfo("Europe/Berlin")


def _now_hint() -> str:
    now = datetime.now(_LOCAL_TZ)
    return f"[Aktuelle Zeit: {now.strftime('%A, %d.%m.%Y, %H:%M Uhr %Z')}]"


class EngineError(Exception):
    """Raised when a turn cannot run (DB/model failures). Name-compatible
    handling: server catches HermesError OR EngineError."""


@dataclass
class EngineProfile:
    """What used to be a Hermes gateway profile."""

    name: str
    model: str
    soul_path: str
    extra_prompt: str = ""
    registry: EntityRegistry | None = None
    think_default: bool = False
    toolbox: Toolbox = field(default_factory=lambda: Toolbox([]))


class EngineClient:
    def __init__(
        self,
        profile: EngineProfile,
        *,
        db_path: str,
        ollama: OllamaChat,
        recorder: TraceRecorder,
        context_window: int | None = None,
    ):
        self._profile = profile
        self._db_path = db_path
        self._ollama = ollama
        self._recorder = recorder
        self._context_window = context_window
        self._soul_cache: tuple[float, str] = (0.0, "")

    @property
    def recorder(self) -> TraceRecorder:
        return self._recorder

    @property
    def profile_name(self) -> str:
        return self._profile.name

    # -- session surface (HermesClient-compatible) --------------------------

    async def create_session(
        self,
        uid: str,
        system_prompt: str | None = None,
        *,
        maintenance: bool = False,
        ephemeral: bool = False,
        model: str = "",
        title: str = "",
    ) -> str:
        session_id = store.create_session(
            self._db_path,
            uid,
            title=title,
            profile=self._profile.name,
            ephemeral=ephemeral,
            maintenance=maintenance,
        )
        if system_prompt:
            store.set_overlay(self._db_path, session_id, system_prompt)
        return session_id

    async def delete_session(self, session_id: str) -> bool:
        store.delete_session(self._db_path, session_id)
        return True

    async def list_sessions(self, uid: str) -> list[dict[str, Any]]:
        return store.list_sessions(self._db_path, uid)

    async def get_session(self, session_id: str, uid: str) -> dict[str, Any] | None:
        return store.get_session(self._db_path, session_id, uid)

    async def set_title(self, session_id: str, uid: str, title: str) -> None:
        store.set_title(self._db_path, session_id, uid, title)

    async def list_toolsets(self) -> list[dict[str, Any]]:
        return [
            {
                "name": self._profile.name,
                "label": f"Sol Engine · {self._profile.name}",
                "description": f"model={self._profile.model}",
                "enabled": True,
                "configured": True,
                "tools": self._profile.toolbox.names(),
            }
        ]

    # -- turns ---------------------------------------------------------------

    async def chat(
        self,
        session_id: str,
        text: str,
        images: list[str] | None = None,
        reasoning_effort: str = "none",
    ) -> str:
        """One turn, non-streamed: drain the stream, return the final answer."""
        answer = ""
        async for event in self.chat_stream(session_id, text, images, reasoning_effort):
            if event["type"] == "run.completed":
                for msg in event["data"].get("messages", []):
                    if msg.get("role") == "assistant" and msg.get("content"):
                        answer = str(msg["content"])
        return answer

    async def chat_stream(
        self,
        session_id: str,
        text: str,
        images: list[str] | None = None,
        reasoning_effort: str = "none",
    ) -> AsyncIterator[dict[str, Any]]:
        owner = store.session_owner(self._db_path, session_id)
        if owner is None:
            raise EngineError(f"unknown session: {session_id}")
        token = current_uid.set(owner)
        try:
            async for event in self._run_turn(
                session_id, text, images, reasoning_effort
            ):
                yield event
        except OllamaError as e:
            log.error("engine.turn.failed", session_id=session_id, error=str(e))
            raise EngineError(str(e)) from e
        finally:
            current_uid.reset(token)

    async def _run_turn(
        self,
        session_id: str,
        text: str,
        images: list[str] | None,
        reasoning_effort: str,
    ) -> AsyncIterator[dict[str, Any]]:
        store.append_message(
            self._db_path, session_id, "user", text, images=images or None
        )
        system = await self._system_prompt(session_id)
        messages = [{"role": "system", "content": system}]
        messages += store.history(self._db_path, session_id)
        think = self._profile.think_default or reasoning_effort not in ("", "none")
        async for event in self._loop(
            messages, think=think, session_id=session_id, persist=True
        ):
            yield event

    async def respond(
        self,
        messages: list[dict[str, Any]],
        *,
        uid: str = "",
        source: str = "assist",
    ) -> AsyncIterator[dict[str, Any]]:
        """Stateless turn for the Ollama facade (HA Assist / gatekeeper).

        The caller owns the conversation history and resends it per turn;
        nothing persists to the store. Incoming system messages (HA's
        configurable prompt) are folded after the profile's own system block,
        and the wall-clock hint rides the last user message — same lever the
        session path uses, and prefix-cache-friendly (the stable soul+registry
        block stays byte-identical across turns).
        """
        token = current_uid.set(uid)
        try:
            system = await self._system_prompt_stateless()
            incoming = [
                str(m.get("content") or "")
                for m in messages
                if m.get("role") == "system" and m.get("content")
            ]
            msgs: list[dict[str, Any]] = [
                {"role": "system", "content": "\n\n".join([system, *incoming])}
            ]
            msgs += [dict(m) for m in messages if m.get("role") != "system"]
            for m in reversed(msgs):
                if m.get("role") == "user":
                    m["content"] = f"{_now_hint()}\n\n{m.get('content') or ''}"
                    break
            async for event in self._loop(
                msgs,
                think=self._profile.think_default,
                session_id=source,
                persist=False,
            ):
                yield event
        except OllamaError as e:
            log.error("engine.respond.failed", source=source, error=str(e))
            raise EngineError(str(e)) from e
        finally:
            current_uid.reset(token)

    async def _loop(
        self,
        messages: list[dict[str, Any]],
        *,
        think: bool,
        session_id: str,
        persist: bool,
    ) -> AsyncIterator[dict[str, Any]]:
        """The agent loop: stream, dispatch tools, feed results back, repeat.

        `persist=False` runs the identical loop without store writes (the
        stateless facade path); traces record either way — session turns under
        their session id, stateless ones under the source label.
        """
        await self._profile.toolbox.prepare()
        tools = self._profile.toolbox.definitions()

        final_content = ""
        final_thinking = ""
        for _ in range(_MAX_PASSES):
            result = None
            async for kind, payload in self._ollama.stream(
                self._profile.model, messages, tools=tools, think=think
            ):
                if kind == "delta":
                    yield {"type": "assistant.delta", "data": {"delta": payload}}
                elif kind == "done":
                    result = payload
            assert result is not None
            self._recorder.record(
                session_id=session_id,
                profile=self._profile.name,
                model=self._profile.model,
                messages=messages,
                tools=tools,
                content=result.content,
                thinking=result.thinking,
                tool_calls=result.tool_calls,
                prompt_tokens=result.prompt_tokens,
                completion_tokens=result.completion_tokens,
                wall_s=result.wall_s,
                context_window=self._context_window,
            )
            if persist:
                store.add_usage(
                    self._db_path,
                    session_id,
                    result.prompt_tokens,
                    result.completion_tokens,
                )
            final_thinking = result.thinking or final_thinking

            if not result.tool_calls:
                final_content = result.content
                break

            # Tool pass: persist the call, dispatch, feed results back.
            if persist:
                store.append_message(
                    self._db_path,
                    session_id,
                    "assistant",
                    result.content,
                    tool_calls=result.tool_calls,
                )
            messages.append(
                {
                    "role": "assistant",
                    "content": result.content,
                    "tool_calls": result.tool_calls,
                }
            )
            for tc in result.tool_calls:
                fn = tc.get("function") or {}
                name = str(fn.get("name") or "")
                args = fn.get("arguments") or {}
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except ValueError:
                        args = {}
                yield {"type": "tool.started", "data": {"tool": name}}
                output = await self._profile.toolbox.dispatch(name, args)
                yield {"type": "tool.completed", "data": {"tool": name}}
                if persist:
                    store.append_message(self._db_path, session_id, "tool", output)
                messages.append({"role": "tool", "content": output, "tool_name": name})
        else:
            # Pass budget exhausted mid-tool-chain: surface what we have.
            final_content = (
                final_content
                or "Entschuldige, das hat zu viele Schritte gebraucht — ich breche hier ab."
            )

        if persist:
            store.append_message(
                self._db_path,
                session_id,
                "assistant",
                final_content,
                reasoning=final_thinking,
            )
        yield {
            "type": "run.completed",
            "data": {
                "messages": [
                    {
                        "role": "assistant",
                        "content": final_content,
                        "reasoning_content": final_thinking,
                    }
                ]
            },
        }

    # -- prompt assembly -----------------------------------------------------

    async def _system_prompt(self, session_id: str) -> str:
        parts = [self._soul()]
        if self._profile.extra_prompt:
            parts.append(self._profile.extra_prompt)
        overlay = store.get_overlay(self._db_path, session_id)
        if overlay:
            parts.append(overlay)
        if self._profile.registry is not None:
            block = await self._profile.registry.prompt_block()
            if block:
                parts.append(block)
        return "\n\n".join(p for p in parts if p.strip())

    async def _system_prompt_stateless(self) -> str:
        """Profile prompt without a session overlay (the facade path)."""
        parts = [self._soul()]
        if self._profile.extra_prompt:
            parts.append(self._profile.extra_prompt)
        if self._profile.registry is not None:
            block = await self._profile.registry.prompt_block()
            if block:
                parts.append(block)
        return "\n\n".join(p for p in parts if p.strip())

    def _soul(self) -> str:
        """SOUL.md, mtime-cached — an edit lands on the next turn, no restart."""
        path = Path(self._profile.soul_path)
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return ""
        if mtime != self._soul_cache[0]:
            self._soul_cache = (
                mtime,
                path.read_text(encoding="utf-8", errors="replace"),
            )
        return self._soul_cache[1]

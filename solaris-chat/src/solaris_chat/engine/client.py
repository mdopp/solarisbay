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
Plus `llm.step` (model + wall_s after each Ollama pass) for the live
activity bubble (#347); `_normalize` folds it to a `step` browser event.
"""

from __future__ import annotations

import contextvars
import json
import re
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from solaris_chat.engine import store
from solaris_chat.engine.bus import SessionBus
from solaris_chat.engine.ollama import OllamaChat, OllamaError
from solaris_chat.engine.registry import EntityRegistry
from solaris_chat.engine.residents import identity_block
from solaris_chat.engine.tools import Toolbox
from solaris_chat.engine.tools import ha as ha_tools
from solaris_chat.engine.trace import TraceRecorder
from solaris_chat.logging import log

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


# Tool discipline, pinned as the LAST system block so it sits closest to the
# history. Position is load-bearing (box A/B 2026-06-12): one stochastic
# narrative reply in the history makes the model imitate it forever after —
# the same rule placed early in the soul lost 0/3 against a poisoned history,
# placed here it reliably restored tool calls. German on purpose: it must
# outweigh German narrative examples in the history.
_TOOL_DISCIPLINE = (
    "Sage NIEMALS nur, dass du etwas tust, lädst oder prüfst. Für jede"
    " Geräteaktion und jede Zustandsfrage rufst du IMMER zuerst das passende"
    " Tool auf und antwortest erst mit dem Ergebnis — auch wenn frühere"
    " Antworten im Verlauf eine Aktion nur angekündigt haben."
    " Bei sicherheitsrelevanten Aktionen, die das Haus öffnen oder sichern —"
    " lock (besonders unlock), alarm_control_panel entschärfen, und cover mit"
    " Geräteklasse garage — fragst du zuerst kurz nach ('Soll ich …?') und"
    " handelst erst nach einem Ja. Alles andere (Licht, Schalter, media_player,"
    " Klima, Ventilator, Szenen, Skripte, normale Rollos/Jalousien) führst du"
    " ohne Rückfrage direkt aus."
)

# A present-tense German device-state assertion ("… ist an", "… ist aus",
# "… ist eingeschaltet", "… läuft", "… ist gesperrt") OR a perfect-tense action
# claim ("habe das Licht eingeschaltet", "Das Licht wurde ausgeschaltet"). When
# the model emits one of these as its final answer WITHOUT having called a tool
# this turn, it is fabricating a result — the clarify→"Ja."→empty-tool_calls
# path (#356) that survives low-temp + the discipline rule. Detection is German
# on purpose: the hot path runs German, and the false-positive surface (a turn
# that merely quotes a state read back from a tool) is excluded by the "no tool
# ran this turn" gate, not by the text. The participle anchor (ge…schaltet) only
# fires on a *completed* action, so an infinitive question ("Soll ich das Licht
# einschalten?") or a future intent ("ich schalte gleich …") does not match.
_DEVICE_CLAIM = re.compile(
    r"\bist\s+(an|aus|ein(geschaltet)?|aus(geschaltet)?|"
    r"gesperrt|entsperrt|gestartet|gestoppt|geschlossen|geöffnet|offen|zu)\b"
    r"|\bist\s+jetzt\b|\bläuft\b"
    # perfect-tense action: habe/hat/haben … (ein|aus|an)geschaltet, with an
    # optional intervening accusative object, or the passive "wurde … geschaltet".
    r"|\b(habe|hat|haben|wurde|wurden)\b[\wäöüß ]*?\b(ein|aus|an)geschaltet\b",
    re.IGNORECASE,
)

# The corrective nudge injected once per turn when a fabricated claim is caught:
# the model asserted an action it never dispatched — force the tool pass.
_CLAIM_CORRECTION = (
    "STOPP: Du hast eine Geräteaktion als erledigt behauptet, aber kein Tool"
    " aufgerufen. Rufe JETZT das passende Tool (ha_call_service) für diese"
    " Aktion auf. Behaupte nichts ohne Tool-Ergebnis."
)


def _is_fabricated_device_claim(content: str) -> bool:
    return bool(_DEVICE_CLAIM.search(content or ""))


class EngineError(Exception):
    """Raised when a turn cannot run (DB/model failures). Name-compatible
    handling: server catches HermesError OR EngineError."""


@dataclass
class EngineProfile:
    """What used to be a Hermes gateway profile."""

    name: str
    model: str
    soul_path: str
    # An optional per-turn model override (#366): when set, its return value
    # (if non-empty) is the model for the next turn, so an admin can re-point
    # the household profile from the panel without a restart. `model` is the
    # static fallback (the configured default).
    model_resolver: Callable[[], str] | None = None
    extra_prompt: str = ""
    registry: EntityRegistry | None = None
    think_default: bool = False
    # The shared household uid (and HA's fallback `user`): a turn carrying this
    # uid is NOT personal, so no resident identity block is injected (#352).
    default_uid: str = "household"
    # Sampling override; None keeps the model's default. The household hot
    # path runs low temperature: at the modelfile default of 1.0 e2b
    # occasionally narrates a device action instead of calling the tool, and
    # one such reply in HA's history self-reinforces (box A/B 2026-06-12).
    temperature: float | None = None
    toolbox: Toolbox = field(default_factory=lambda: Toolbox([]))
    # Guest profile (#353): a turn runs statelessly — nothing is written to the
    # store, so no guest session, history or fact survives the conversation.
    ephemeral: bool = False


class EngineClient:
    def __init__(
        self,
        profile: EngineProfile,
        *,
        db_path: str,
        ollama: OllamaChat,
        recorder: TraceRecorder,
        context_window: int | None = None,
        bus: SessionBus | None = None,
    ):
        self._profile = profile
        self._db_path = db_path
        self._ollama = ollama
        self._recorder = recorder
        self._context_window = context_window
        self._bus = bus
        self._soul_cache: tuple[float, str] = (0.0, "")

    @property
    def recorder(self) -> TraceRecorder:
        return self._recorder

    @property
    def profile_name(self) -> str:
        return self._profile.name

    def _model(self) -> str:
        """The model for this turn: the profile's resolver override (#366) if it
        yields a non-empty tag, else the static `profile.model` default."""
        resolver = self._profile.model_resolver
        return (resolver() if resolver else "") or self._profile.model

    @property
    def ephemeral(self) -> bool:
        return self._profile.ephemeral

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

    async def delete_session(self, session_id: str, uid: str) -> bool:
        return store.delete_session(self._db_path, session_id, uid)

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
                "label": f"Solaris Engine · {self._profile.name}",
                "description": f"model={self._model()}",
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
            # The SSE heartbeat consumes each generator step in its own task,
            # so this finally can run in a foreign context (box-observed:
            # ValueError tore down the stream as a Network error).
            try:
                current_uid.reset(token)
            except ValueError:
                pass

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
        # Bound the durable household chat in place: it is never forked (#419),
        # so when its history outgrows the window the oldest turns are dropped
        # (#420). Soul + device registry are the per-turn system prompt below —
        # never touched; only chat turns are cut. Other sessions are bounded by
        # continuation-compaction (server.maybe_compact), not here.
        owner = store.session_owner(self._db_path, session_id)
        if owner and session_id == store.household_session_id(owner):
            store.truncate_session_head(
                self._db_path, session_id, int((self._context_window or 32768) * 0.4)
            )
        system = await self._system_prompt(session_id)
        messages = [{"role": "system", "content": system}]
        messages += store.history(self._db_path, session_id)
        think = self._profile.think_default or reasoning_effort not in ("", "none")
        owner = store.session_owner(self._db_path, session_id) or ""
        # Mirror the inbound transcript to this session's OTHER open tabs (#344)
        # before any token streams — a tab that didn't originate the turn (voice,
        # or another browser) renders the user bubble as soon as it lands.
        self._mirror(session_id, owner, "mirror_user", {"text": text})
        async for event in self._loop(
            messages, think=think, session_id=session_id, persist=True, uid=owner
        ):
            self._mirror(session_id, owner, "mirror_event", event)
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
            # Recency is load-bearing (box A/B): the tool-discipline rule must
            # be the LAST system content — after the caller's prompt, which
            # otherwise outweighs it again ("Antworte kurz" → narration).
            tail = [_TOOL_DISCIPLINE] if self._profile.toolbox.names() else []
            msgs: list[dict[str, Any]] = [
                {"role": "system", "content": "\n\n".join([system, *incoming, *tail])}
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
                uid=uid,
            ):
                yield event
        except OllamaError as e:
            log.error("engine.respond.failed", source=source, error=str(e))
            raise EngineError(str(e)) from e
        finally:
            # A client that drops the stream closes this generator from a
            # different asyncio context — the reset token is then foreign
            # (box-observed ValueError on an aborted HA turn).
            try:
                current_uid.reset(token)
            except ValueError:
                pass

    async def respond_session(
        self,
        text: str,
        *,
        uid: str,
    ) -> AsyncIterator[dict[str, Any]]:
        """A voice turn into the resident's durable household session (#345).

        Where `respond` is stateless (HA owns the history), this persists into
        the shared household session — the same row the browser opens — so
        spoken and typed history are one conversation. HA still resends its
        full message list, but the store is now the source of truth, so only
        the latest user `text` is run; the soul/registry block is the session's
        own (the caller's per-call system prompt is dropped — the durable
        session already carries the engine's identity)."""
        session_id = store.ensure_household_session(self._db_path, uid)
        # The wall-clock hint rides the user turn (the session path has no
        # topic-hint wrapper) — same lever the browser turns get server-side.
        turn = f"{_now_hint()}\n\n{text}" if text else text
        async for event in self.chat_stream(session_id, turn):
            yield event

    async def _loop(
        self,
        messages: list[dict[str, Any]],
        *,
        think: bool,
        session_id: str,
        persist: bool,
        uid: str = "",
    ) -> AsyncIterator[dict[str, Any]]:
        """The agent loop: stream, dispatch tools, feed results back, repeat.

        `persist=False` runs the identical loop without store writes (the
        stateless facade path); traces record either way — session turns under
        their session id, stateless ones under the source label.
        """
        await self._profile.toolbox.prepare()
        tools = self._profile.toolbox.definitions()
        # Per-turn sink the HA state tools fill with read-only card-specs (#475);
        # drained into a `ha_cards` event at turn end.
        ha_cards: list[dict[str, Any]] = []
        ha_tools.card_sink.set(ha_cards)
        options = (
            {"temperature": self._profile.temperature}
            if self._profile.temperature is not None
            else None
        )

        has_tools = bool(self._profile.toolbox.names())
        tool_dispatched = False
        corrected = False
        final_content = ""
        final_thinking = ""
        model = self._model()
        for _ in range(_MAX_PASSES):
            result = None
            async for kind, payload in self._ollama.stream(
                model, messages, tools=tools, think=think, options=options
            ):
                if kind == "delta":
                    yield {"type": "assistant.delta", "data": {"delta": payload}}
                elif kind == "done":
                    result = payload
            assert result is not None
            self._recorder.record(
                session_id=session_id,
                profile=self._profile.name,
                model=model,
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
            yield {
                "type": "llm.step",
                "data": {"model": model, "wall_s": result.wall_s},
            }

            if not result.tool_calls:
                # Fabrication guard (#356): the model claims a device action is
                # done but dispatched no tool this turn. Re-prompt once to force
                # the tool pass instead of accepting the fabricated success.
                if (
                    has_tools
                    and not tool_dispatched
                    and not corrected
                    and _is_fabricated_device_claim(result.content)
                ):
                    corrected = True
                    messages.append({"role": "assistant", "content": result.content})
                    messages.append({"role": "system", "content": _CLAIM_CORRECTION})
                    continue
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
                tool_dispatched = True
                yield {"type": "tool.started", "data": {"tool": name}}
                # Re-pin the turn's resident here, IN the dispatching task:
                # the SSE heartbeat runs each generator step in its own task,
                # which inherits the handler context without the turn's
                # set() — tools would otherwise see the default uid from
                # pass 2 on (timers/facts written ownerless).
                if uid:
                    current_uid.set(uid)
                # Re-pin the sink too: the heartbeat task may not inherit the
                # _loop set() (same reason as current_uid above), so a card read
                # during dispatch would otherwise land nowhere (#475).
                ha_tools.card_sink.set(ha_cards)
                t0 = time.monotonic()
                output = await self._profile.toolbox.dispatch(name, args)
                tool_wall_s = time.monotonic() - t0
                self._recorder.record_tool(
                    session_id=session_id,
                    profile=self._profile.name,
                    tool_name=name,
                    wall_s=tool_wall_s,
                )
                yield {
                    "type": "tool.completed",
                    "data": {"tool": name, "wall_s": tool_wall_s},
                }
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
        if ha_cards:
            yield {"type": "ha_cards", "data": {"cards": ha_cards}}
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
        resident = identity_block(current_uid.get(), self._profile.default_uid)
        if resident:
            parts.append(resident)
        overlay = store.get_overlay(self._db_path, session_id)
        if overlay:
            parts.append(overlay)
        if self._profile.registry is not None:
            block = await self._profile.registry.prompt_block()
            if block:
                parts.append(block)
        if self._profile.toolbox.names():
            parts.append(_TOOL_DISCIPLINE)
        return "\n\n".join(p for p in parts if p.strip())

    async def _system_prompt_stateless(self) -> str:
        """Profile prompt without a session overlay (the facade path). The
        tool-discipline tail is appended by respond() AFTER the caller's
        system prompt — recency is load-bearing."""
        parts = [self._soul()]
        if self._profile.extra_prompt:
            parts.append(self._profile.extra_prompt)
        resident = identity_block(current_uid.get(), self._profile.default_uid)
        if resident:
            parts.append(resident)
        if self._profile.registry is not None:
            block = await self._profile.registry.prompt_block()
            if block:
                parts.append(block)
        return "\n\n".join(p for p in parts if p.strip())

    def _mirror(
        self, session_id: str, uid: str, kind: str, event: dict[str, Any]
    ) -> None:
        """Publish one turn event to this session's other open tabs (#344).

        No-op without a bus (offline tests) or an owner. The originating request
        keeps its own direct stream; subscribers are every OTHER open client of
        the same (session, uid)."""
        if self._bus is not None and uid:
            self._bus.publish(session_id, uid, {"kind": kind, "event": event})

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

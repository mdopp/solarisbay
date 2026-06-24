"""Ollama-compatible facade — the engine as a Home Assistant conversation agent.

HA 2026.6's core `openai_conversation` integration has no custom base_url, but
its `ollama` integration takes a free URL + optional Bearer api_key and speaks
exactly the protocol the engine already uses downstream. So the engine exposes
a minimal Ollama surface under `/ollama` on the chat port:

  GET  /ollama/api/tags     — the config-flow validation call (`client.list()`)
  GET  /ollama/api/version  — cheap liveness some ollama clients ping
  POST /ollama/api/chat     — the conversation call, NDJSON-streamed or single

"Models" are engine profiles: `solaris` (household, fast) and `solaris-deep` (12b,
thinks). HA resends its conversation history per turn; the engine runs its
own tool loop server-side and streams only content deltas back — HA never
sees tool_calls, so its MAX_TOOL_ITERATIONS loop runs exactly once. The
voice-gatekeeper speaks the same surface (stream=false) for wyoming-satellite
hardware.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from aiohttp import web

from solaris_chat import trace_store
from solaris_chat.engine import store
from solaris_chat.engine.client import EngineClient, EngineError
from solaris_chat.logging import log
from solaris_chat.voice_uid_stash import consume_uid

# The gatekeeper stashes this uid for a speaker that speaker-ID heard but
# matched to no enrolled resident (an attempted-but-unknown speaker, #351).
# It is not a real resident: the turn runs the ephemeral guest profile (#353).
GUEST_UID = "guest"


def _model_entry(name: str) -> dict[str, Any]:
    # Enough fields for the ollama python client's pydantic ListResponse.
    return {
        "name": name,
        "model": name,
        "modified_at": "2026-01-01T00:00:00Z",
        "size": 0,
        "digest": "solaris-engine",
        "details": {"family": "solaris", "parameter_size": "", "format": ""},
    }


def _authorized(request: web.Request, api_key: str) -> bool:
    if not api_key:
        return True
    return request.headers.get("Authorization", "") == f"Bearer {api_key}"


# HA's `ollama` conversation integration derives `continue_conversation` purely
# from whether the assistant's reply text ends in a question mark (chat_log.py
# continue_conversation ← util.py). So when THIS turn offered a follow-up via
# offer_choices (the `quick_replies` event), the spoken text must end in `?` for
# the Voice PE to re-open the mic without a re-wake (#566). A normal statement
# answer must NOT, so the loop stops.
_QUESTION_MARKS = ("?", "？", ";")


def _as_question(text: str) -> str:
    return text if text.rstrip().endswith(_QUESTION_MARKS) else text.rstrip() + "?"


def _chunk(model: str, content: str, done: bool, done_reason: str = "") -> bytes:
    body: dict[str, Any] = {
        "model": model,
        "created_at": datetime.now(UTC).isoformat(),
        "message": {"role": "assistant", "content": content},
        "done": done,
    }
    if done:
        body["done_reason"] = done_reason or "stop"
    return (json.dumps(body, ensure_ascii=False) + "\n").encode("utf-8")


def add_facade_routes(
    app: web.Application,
    *,
    clients: dict[str, EngineClient],
    api_key: str,
    default_uid: str,
    solaris_db_path: str,
) -> None:
    async def tags(request: web.Request) -> web.Response:
        if not _authorized(request, api_key):
            return web.json_response({"error": "unauthorized"}, status=401)
        return web.json_response({"models": [_model_entry(name) for name in clients]})

    async def version(request: web.Request) -> web.Response:
        if not _authorized(request, api_key):
            return web.json_response({"error": "unauthorized"}, status=401)
        return web.json_response({"version": "solaris-engine"})

    async def chat(request: web.Request) -> web.StreamResponse:
        if not _authorized(request, api_key):
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 — any malformed JSON
            return web.json_response({"error": "invalid json"}, status=400)
        model = str(body.get("model") or "")
        client = clients.get(model.removesuffix(":latest"))
        if client is None:
            return web.json_response(
                {"error": f"model '{model}' not found"}, status=404
            )
        messages = body.get("messages")
        if not isinstance(messages, list) or not messages:
            return web.json_response({"error": "messages required"}, status=400)
        stream = body.get("stream", True)
        # The latest user utterance doubles as the lookup key for the live
        # voice path: when the gatekeeper served as HA's STT provider it
        # stashed {transcript -> resolved resident uid} (#350, approach b).
        # Resolve the speaking resident by that transcript; fall back to the
        # body's `user` (HA sends `household`) on a miss. Consume-once.
        transcript = _last_user(messages)
        uid = consume_uid(solaris_db_path, transcript) or str(
            body.get("user") or default_uid
        )
        # An unknown speaker (speaker-ID ran but matched no resident, #351) is
        # routed to the ephemeral guest profile, not the resident's household
        # session — only when speaker-ID actively resolved UNKNOWN, never on a
        # plain stash miss (speaker-ID off / not attempted), which stays
        # household. Falls through to the requested model if no guest profile
        # is wired (it ships as its own model first; #353).
        guest = clients.get("solaris-guest")
        if uid == GUEST_UID and guest is not None:
            model, client = "solaris-guest", guest
        log.info("engine.facade.turn", model=model, uid=uid, n_messages=len(messages))

        # A voice turn lands in the resident's durable household session (#345):
        # the store owns the history, so only the latest user utterance is run
        # (HA still replays its whole list — we take the tail). The same session
        # the browser opens, so spoken + typed history are one conversation and
        # the turn mirrors live into open tabs (#344) via the persisted path.
        # A guest profile (#353) is ephemeral: it runs the stateless `respond`
        # path on HA's replayed history, so nothing about the guest persists.
        text = transcript

        # A durable voice turn persists its trace into the same household
        # session (#405) so the "Zuhause" chat carries the same per-turn trace
        # rows the browser path writes — not just message history. Ephemeral
        # guest turns persist nothing.
        t0 = time.time()

        # HA's conversation id keys the confirmation gate per conversation on the
        # ephemeral path so one caller can't confirm another's held action (#570
        # F3); None when absent disables stashing (re-gate every turn).
        conversation_id = body.get("conversation_id")
        conversation_id = str(conversation_id) if conversation_id else None

        def turns() -> AsyncIterator[dict[str, Any]]:
            if client.ephemeral:
                return client.respond(
                    messages, uid=uid, source=model, conversation_id=conversation_id
                )
            return client.respond_session(text, uid=uid)

        def persist_trace() -> None:
            if client.ephemeral:
                return
            _persist_voice_trace(solaris_db_path, client, uid, t0)

        if not stream:
            try:
                answer, offered_choices = await _drain(turns())
            except EngineError:
                persist_trace()
                return web.json_response({"error": "engine unavailable"}, status=502)
            persist_trace()
            if offered_choices and answer:
                answer = _as_question(answer)
            return web.Response(
                body=_chunk(model, answer, done=True),
                content_type="application/json",
            )

        resp = web.StreamResponse(headers={"Content-Type": "application/x-ndjson"})
        await resp.prepare(request)
        streamed = ""
        offered_choices = False
        try:
            async for event in turns():
                if event["type"] == "assistant.delta":
                    delta = str(event["data"].get("delta") or "")
                    if delta:
                        streamed += delta
                        await resp.write(_chunk(model, delta, done=False))
                elif event["type"] == "quick_replies":
                    offered_choices = True
                elif event["type"] == "run.completed":
                    final = _final_answer(event)
                    # A tool turn can finish with no streamed deltas — surface
                    # the final answer as one late chunk (the #258 pattern).
                    if final and not streamed.strip():
                        streamed = final
                        await resp.write(_chunk(model, final, done=False))
            # A follow-up turn (offer_choices) must end in `?` so HA keeps the
            # mic open for the answer without a re-wake (#566). Deltas already
            # went out verbatim — append the missing `?` as a trailing chunk.
            if (
                offered_choices
                and streamed.strip()
                and not streamed.rstrip().endswith(_QUESTION_MARKS)
            ):
                await resp.write(_chunk(model, "?", done=False))
        except EngineError as e:
            log.error("engine.facade.failed", model=model, error=str(e))
            # A failed voice turn still persists whatever the recorder captured
            # before the error (#562) — otherwise the failure is invisible in
            # the chat UI, the operator's exact complaint about intent-failed.
            persist_trace()
            await resp.write(_chunk(model, "", done=True, done_reason="error"))
            return resp
        persist_trace()
        await resp.write(_chunk(model, "", done=True))
        return resp

    app.router.add_get("/ollama/api/tags", tags)
    app.router.add_get("/ollama/api/version", version)
    app.router.add_post("/ollama/api/chat", chat)


def _persist_voice_trace(
    db_path: str, client: EngineClient, uid: str, t0: float
) -> None:
    """Persist a durable voice turn's trace into its household session (#405).

    Mirrors the browser path's `persist_turn_trace`: the recorder's steps for
    this session since `t0` become `session_traces` rows, so the "Zuhause" chat
    reopens with the same per-turn trace the typed path shows. Best-effort — a
    trace-write hiccup never breaks a voice turn that already replied."""
    session_id = store.household_session_id(uid)
    try:
        trace_id = uuid.uuid4().hex
        steps = []
        for order, rec in enumerate(client.recorder.for_session(session_id, t0)):
            # Persist the detail body with the step under a stable per-step key
            # so the modal resolves after a reload/restart (#451).
            detail = client.recorder.detail(rec["id"]) if "id" in rec else None
            steps.append(
                {
                    "model": rec.get("model"),
                    "profile": rec.get("profile"),
                    "wall_s": rec.get("wall_s"),
                    "prompt_tokens": rec.get("prompt_tokens"),
                    "completion_tokens": rec.get("completion_tokens"),
                    "context_free": rec.get("context_free"),
                    "finish_reason": rec.get("finish_reason"),
                    "n_tools": rec.get("n_tools"),
                    "detail_id": f"{trace_id}:{order}" if detail else None,
                    "step_kind": rec.get("step_kind"),
                    "tool_name": rec.get("tool_name"),
                    "detail_json": json.dumps(detail) if detail else None,
                }
            )
        if steps:
            trace_store.persist_trace(db_path, session_id, trace_id, uid, steps)
    except Exception as e:  # noqa: BLE001 — trace persistence is best-effort
        log.warn("engine.facade.trace_persist_error", uid=uid, error=str(e))


def _last_user(messages: list[Any]) -> str:
    """The latest user utterance in HA's replayed message list (#345). The
    durable session owns the rest of the history, so only the tail is run."""
    for msg in reversed(messages):
        if isinstance(msg, dict) and msg.get("role") == "user" and msg.get("content"):
            return str(msg["content"])
    return ""


async def _drain(turns: AsyncIterator[dict[str, Any]]) -> tuple[str, bool]:
    answer = ""
    streamed = ""
    offered_choices = False
    async for event in turns:
        if event["type"] == "assistant.delta":
            streamed += str(event["data"].get("delta") or "")
        elif event["type"] == "quick_replies":
            offered_choices = True
        elif event["type"] == "run.completed":
            answer = _final_answer(event)
    return answer or streamed, offered_choices


def _final_answer(event: dict[str, Any]) -> str:
    for msg in event.get("data", {}).get("messages", []):
        if msg.get("role") == "assistant" and msg.get("content"):
            return str(msg["content"])
    return ""

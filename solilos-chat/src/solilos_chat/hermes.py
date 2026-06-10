"""Thin client for Hermes' native session API.

Contract (NOT the gatekeeper's placeholder `/converse`):
  - `POST /api/sessions`                 -> create a session, returns `{"id": ...}`
  - `GET  /api/sessions`                 -> `{"data": [{id, title, preview, last_active, ...}]}`
  - `GET  /api/sessions/{id}`            -> `{"session": {...}}` (summary, no messages)
  - `GET  /api/sessions/{id}/messages`   -> `{"data": [{role, content, ...}]}`
  - `PATCH /api/sessions/{id}`           -> body `{"title": ...}`, sets the title
  - `POST /api/sessions/{id}/chat`       -> body `{"input": ...}`, returns the reply
  - `POST /api/sessions/{id}/chat/stream`-> body `{"input": ...}`, SSE event stream

Auth is a bearer token (`API_SERVER_KEY`) held server-side; the browser
never sees it.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import aiohttp

from solilos_chat import marker
from solilos_chat.logging import log


class HermesError(Exception):
    """Raised when Hermes returns a non-2xx response."""


class HermesClient:
    def __init__(self, base_url: str, token: str, timeout: float = 120.0):
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = aiohttp.ClientTimeout(total=timeout)

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

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
        """Create a session bound to `uid`; return its id.

        `system_prompt` is the chosen personality's overlay (see
        `personalities.py`); Hermes accepts it only at create time (PATCH
        rejects it). Empty/None => no overlay, pure SOUL.md.

        `model` (latency bundle) is the Ollama tag this session binds to —
        Hermes accepts `model` only at session create, so adaptive routing
        (Schnell→e2b / Gründlich→12b) is fixed for the session's lifetime here.
        Empty => no override, Hermes' configured default model.

        The title is seeded with the caller's immutable uid marker (#153) so a
        session is owned (and visible only to its resident) from the moment it
        exists, before the first turn re-titles it. `user_id` is still POSTed for
        forward-compat, but Hermes v0.16.0 does not persist it — the marker is
        the real ownership record.

        `maintenance` (#229) seeds the `[maint:<hash>] ` marker instead, which
        keeps a ServiceBay-maintenance session out of the household list (its
        marker is in a different namespace from the household `[uid:...]` filter).

        `ephemeral` (#246) seeds the `[temp:<hash>] ` marker, which likewise keeps
        an incognito chat out of the durable household list — it is deleted on
        close, so it must never appear as a persisted session.

        `title` appends a human suffix after the household `[uid:...]` marker
        (#267). The default bare marker is fine for a session that is re-titled
        from its first turn, but Hermes enforces title uniqueness, so a caller
        that opens a session which may never be re-titled (e.g. a compaction
        continuation) must pass a unique suffix or risk a 400 "title already in
        use" against an abandoned bare-marker stub. For an ephemeral session the
        suffix rides *after* the `[temp:<hash>] ` marker (the marker prefix is
        anchored, so the incognito/not-listed semantics are preserved) — needed
        so two temp chats for the same resident can't collide (#286). Ignored for
        the maintenance marker.
        """
        url = f"{self._base_url}/api/sessions"

        def _session_title(suffix: str) -> str:
            # `suffix` disambiguates a title collision (#301): Hermes enforces
            # globally-unique titles, so two chats whose first message is the
            # SAME text (e.g. "Welche Lichter sind an?") would 400 "title already
            # in use" — a real resident-facing "(no reply)" in the household chat.
            # The suffix rides AFTER the human title so the marker prefix
            # (ownership / temp / maint) stays anchored.
            if ephemeral:
                return marker.temp_marker(uid) + title + suffix
            if maintenance:
                return marker.maint_marker(uid) + suffix
            if title:
                return marker.embed(uid, title + suffix)
            return marker.marker_for(uid) + suffix

        suffix = ""
        for attempt in range(3):
            payload: dict[str, Any] = {"user_id": uid, "title": _session_title(suffix)}
            if system_prompt:
                payload["system_prompt"] = system_prompt
            if model:
                payload["model"] = model
            async with aiohttp.ClientSession(timeout=self._timeout) as client:
                async with client.post(
                    url, json=payload, headers=self._headers()
                ) as resp:
                    status = resp.status
                    text = await resp.text()
            if status == 400 and ("already in use" in text or "invalid_title" in text):
                # Retry with a unique suffix appended to the human title.
                suffix = f" ({uuid.uuid4().hex[:6]})"
                log.info("chat.session.title_retry", uid=uid, attempt=attempt + 1)
                continue
            if status >= 400:
                log.error(
                    "chat.hermes.error",
                    op="create_session",
                    status=status,
                    body=text[:500],
                )
                raise HermesError(f"create_session: Hermes returned {status}")
            session_id = _extract_session_id(json.loads(text) if text else {})
            if not session_id:
                raise HermesError("create_session: no session id in response")
            return session_id
        raise HermesError("create_session: title collision unresolved after retries")

    async def list_toolsets(self) -> list[dict[str, Any]]:
        """List Hermes' toolsets (`GET /v1/toolsets`) — built-ins + MCP
        servers, each `{name, label, description, enabled, configured,
        tools[]}`. Empty list on failure."""
        url = f"{self._base_url}/v1/toolsets"
        async with aiohttp.ClientSession(timeout=self._timeout) as client:
            async with client.get(url, headers=self._headers()) as resp:
                if resp.status >= 400:
                    detail = (await resp.text())[:300]
                    log.error(
                        "chat.hermes.error",
                        op="list_toolsets",
                        status=resp.status,
                        body=detail,
                    )
                    return []
                body = await resp.json()
        data = body.get("data") if isinstance(body, dict) else None
        return data if isinstance(data, list) else []

    async def delete_session(self, session_id: str) -> bool:
        """Delete a session. True on 2xx (or 404 — already gone is fine)."""
        url = f"{self._base_url}/api/sessions/{session_id}"
        async with aiohttp.ClientSession(timeout=self._timeout) as client:
            async with client.delete(url, headers=self._headers()) as resp:
                if resp.status < 400 or resp.status == 404:
                    return True
                detail = (await resp.text())[:300]
                log.error(
                    "chat.hermes.error",
                    op="delete_session",
                    status=resp.status,
                    body=detail,
                )
                return False

    async def list_sessions(self, uid: str) -> list[dict[str, Any]]:
        """List the caller's sessions, scoped by the uid title-marker (#153).

        Per-resident isolation: Hermes v0.16.0 stores `user_id: null` (the
        `user_id` we POST on create is not persisted or returned), so ownership
        is carried by an immutable `[uid:<hash>] ` prefix the proxy embeds in
        the title. We keep only sessions whose title bears the *caller's* marker
        and strip the marker before returning (so #155's human titles render
        clean). Unmarked legacy sessions are hidden (privacy-safe: they cannot
        be attributed to a resident). Each item is `{id, title, last_activity}`.
        """
        url = f"{self._base_url}/api/sessions"
        async with aiohttp.ClientSession(timeout=self._timeout) as client:
            async with client.get(url, headers=self._headers()) as resp:
                body = await self._json_or_raise(resp, "list_sessions")
        out: list[dict[str, Any]] = []
        for raw in _iter_session_items(body):
            if not marker.has_marker(uid, str(raw.get("title") or "")):
                continue
            summary = _session_summary(raw)
            summary["title"] = marker.strip(summary["title"])
            out.append(summary)
        return out

    async def get_session(self, session_id: str, uid: str) -> dict[str, Any] | None:
        """Fetch a session summary + its message history, owner-scoped (#153).

        The session endpoint returns only a summary; messages live on a
        separate `/messages` endpoint, so we fetch both.

        Ownership is enforced by the title marker: a session whose title does
        not carry the caller's `[uid:<hash>] ` prefix returns `None` (same as a
        missing id), so one resident cannot open another's history by guessing
        an id. The marker is stripped from the returned title for the UI.
        """
        url = f"{self._base_url}/api/sessions/{session_id}"
        async with aiohttp.ClientSession(timeout=self._timeout) as client:
            async with client.get(url, headers=self._headers()) as resp:
                if resp.status == 404:
                    return None
                body = await self._json_or_raise(resp, "get_session")
            session = body.get("session") if isinstance(body, dict) else None
            raw = session if isinstance(session, dict) else body
            if not isinstance(raw, dict):
                return None
            if not marker.has_marker(uid, str(raw.get("title") or "")):
                return None
            msg_url = f"{self._base_url}/api/sessions/{session_id}/messages"
            async with client.get(msg_url, headers=self._headers()) as resp:
                msg_body = await self._json_or_raise(resp, "get_messages")
        summary = _session_summary(raw)
        summary["title"] = marker.strip(summary["title"])
        summary["messages"] = _extract_messages(msg_body)
        return summary

    async def set_title(self, session_id: str, uid: str, title: str) -> None:
        """Persist a session title (PATCH), re-injecting the uid marker (#153).

        The marker prefix is always re-embedded so an auto-derived title from
        the first chat turn carries the same ownership tag as the create-time
        seed; this is the only title-write path (no browser PATCH route exists),
        which is what keeps the marker immutable to browser clients. Silent on a
        non-2xx — a title is a nicety, not worth failing the turn it rides on."""
        url = f"{self._base_url}/api/sessions/{session_id}"
        async with aiohttp.ClientSession(timeout=self._timeout) as client:
            async with client.patch(
                url, json={"title": marker.embed(uid, title)}, headers=self._headers()
            ) as resp:
                if resp.status >= 400:
                    detail = (await resp.text())[:300]
                    log.error(
                        "chat.hermes.error",
                        op="set_title",
                        status=resp.status,
                        body=detail,
                    )

    async def chat(
        self,
        session_id: str,
        text: str,
        images: list[str] | None = None,
        reasoning_effort: str = "none",
    ) -> str:
        """Send one turn to an existing session; return the reply text.

        `images` are `data:image/...;base64,...` URLs (camera/upload from the
        chat panel, #183); folded into `input` as OpenAI content parts — the
        shape Hermes session-chat actually consumes (#202) — so a vision model
        can act on the attachment.

        `reasoning_effort` (#222) is the per-turn knob: "none" (fast, no
        thinking generated) by default; "low"/"high" make the model reason and
        the UI render the block.
        """
        url = f"{self._base_url}/api/sessions/{session_id}/chat"
        async with aiohttp.ClientSession(timeout=self._timeout) as client:
            async with client.post(
                url,
                json=_chat_body(text, images, reasoning_effort),
                headers=self._headers(),
            ) as resp:
                body = await self._json_or_raise(resp, "chat")
        return _extract_reply(body)

    async def chat_stream(
        self,
        session_id: str,
        text: str,
        images: list[str] | None = None,
        reasoning_effort: str = "none",
    ) -> AsyncIterator[dict[str, Any]]:
        """Stream one turn; yield parsed Hermes SSE events as dicts.

        Each yielded event is `{"type": <event>, "data": <decoded payload>}`.
        The `assistant.delta` event carries token deltas; `tool.started`/
        `tool.completed` carry tool names; `run.completed` ends the turn.
        `images` (data URLs, #183) ride `input` as OpenAI content parts (#202).
        """
        url = f"{self._base_url}/api/sessions/{session_id}/chat/stream"
        async with aiohttp.ClientSession(timeout=self._timeout) as client:
            async with client.post(
                url,
                json=_chat_body(text, images, reasoning_effort),
                headers=self._headers(),
            ) as resp:
                if resp.status >= 400:
                    detail = (await resp.text())[:500]
                    log.error(
                        "chat.hermes.error",
                        op="chat_stream",
                        status=resp.status,
                        body=detail,
                    )
                    raise HermesError(f"chat_stream: Hermes returned {resp.status}")
                async for event in _iter_sse(resp.content):
                    yield event

    @staticmethod
    async def _json_or_raise(resp: aiohttp.ClientResponse, op: str) -> Any:
        if resp.status >= 400:
            detail = (await resp.text())[:500]
            log.error("chat.hermes.error", op=op, status=resp.status, body=detail)
            raise HermesError(f"{op}: Hermes returned {resp.status}")
        return await resp.json()


async def _iter_sse(stream: aiohttp.StreamReader) -> AsyncIterator[dict[str, Any]]:
    """Parse an SSE byte stream into `{"type", "data"}` events.

    Frames are blank-line separated; we collect `event:` and (possibly
    multi-line) `data:` fields, JSON-decoding the data when it parses.
    """
    event = ""
    data_lines: list[str] = []
    async for raw in stream:
        line = raw.decode("utf-8", "replace").rstrip("\r\n")
        if line == "":
            if data_lines or event:
                payload = "\n".join(data_lines)
                yield {"type": event or "message", "data": _maybe_json(payload)}
            event = ""
            data_lines = []
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        value = value[1:] if value.startswith(" ") else value
        if field == "event":
            event = value
        elif field == "data":
            data_lines.append(value)
    if data_lines or event:
        payload = "\n".join(data_lines)
        yield {"type": event or "message", "data": _maybe_json(payload)}


def _chat_body(
    text: str, images: list[str] | None, reasoning_effort: str = "none"
) -> dict[str, Any]:
    """Build the Hermes chat body.

    Text-only turns send `input` as a plain string (request shape identical to
    before). When images are present, `input` becomes an OpenAI-style
    content-parts array — a `{"type": "text"}` part followed by one
    `{"type": "image_url", "image_url": {"url": <data:image/...;base64,...>}}`
    part per image. This is the *only* shape Hermes' session-chat consumes:
    `_session_chat_user_message` reads `message`/`input` and runs it through
    `_normalize_multimodal_content`, which requires full `data:image/...` URLs
    (the `data:` prefix must stay) and ignores any top-level `images` key (#202).

    `reasoning_effort` (#222) rides the body per turn. When it is a real
    reasoning level (not "none") we also set `show_reasoning: true` (#224) so
    Hermes surfaces the thinking block — the live config has it off, so without
    this the UI would have nothing to render. A fast ("none") turn sends neither
    `show_reasoning` nor a thinking block, so it stays clean.
    """
    if not images:
        body: dict[str, Any] = {"input": text}
    else:
        parts: list[dict[str, Any]] = [{"type": "text", "text": text}]
        for img in images:
            parts.append({"type": "image_url", "image_url": {"url": img}})
        body = {"input": parts}
    body["reasoning_effort"] = reasoning_effort
    if reasoning_effort != "none":
        body["show_reasoning"] = True
    return body


def _maybe_json(payload: str) -> Any:
    try:
        return json.loads(payload)
    except (ValueError, TypeError):
        return payload


def _extract_session_id(body: Any) -> str:
    if not isinstance(body, dict):
        return ""
    # Tolerate a couple of envelope shapes Hermes may use.
    session = body.get("session") if isinstance(body.get("session"), dict) else body
    return str(session.get("id") or session.get("session_id") or "")


def _iter_session_items(body: Any) -> list[Any]:
    """Pull the session list out of whatever envelope Hermes returns."""
    if isinstance(body, list):
        return body
    if isinstance(body, dict):
        for key in ("sessions", "items", "data", "results"):
            value = body.get(key)
            if isinstance(value, list):
                return value
    return []


def _session_owner(raw: Any) -> str:
    if not isinstance(raw, dict):
        return ""
    return str(raw.get("user_id") or raw.get("owner") or raw.get("source") or "")


def _session_summary(raw: dict[str, Any]) -> dict[str, Any]:
    """Fold a Hermes session item into the browser-facing summary.

    Title is null for chat-created sessions, so we surface `preview` (the
    first user message, supplied by the list endpoint) as a derived label
    the page can fall back to. `last_active` is an epoch float — emitted as
    a string the page parses as either ISO or epoch seconds.
    """
    sid = str(raw.get("id") or raw.get("session_id") or "")
    title = str(raw.get("title") or raw.get("name") or "").strip()
    preview = str(raw.get("preview") or "").strip()
    last = (
        raw.get("last_active")
        or raw.get("last_activity")
        or raw.get("updated_at")
        or raw.get("started_at")
        or raw.get("created_at")
        or ""
    )
    return {
        "id": sid,
        "title": title,
        "preview": preview,
        "last_activity": str(last or ""),
        # Token/cost accounting for the /context command (Hermes per-session
        # totals; absent on list items, present on the single-session fetch).
        "input_tokens": raw.get("input_tokens"),
        "output_tokens": raw.get("output_tokens"),
        "message_count": raw.get("message_count"),
        "estimated_cost_usd": raw.get("estimated_cost_usd"),
    }


def _extract_messages(body: dict[str, Any]) -> list[dict[str, str]]:
    """Normalise a `/messages` payload to `[{role, content}]`.

    Hermes returns `{"object": "list", "data": [...]}`; we tolerate a bare
    list or a `messages` key too.
    """
    messages = body.get("data")
    if not isinstance(messages, list):
        messages = body.get("messages")
    if not isinstance(messages, list):
        return []
    out: list[dict[str, str]] = []
    for m in messages:
        if not isinstance(m, dict):
            continue
        role = str(m.get("role") or "")
        content = m.get("content")
        if isinstance(content, list):
            content = "".join(
                str(p.get("text") or "") if isinstance(p, dict) else str(p)
                for p in content
            )
        text = str(content or "")
        if role and text:
            out.append({"role": role, "content": text})
    return out


def _extract_reply(body: Any) -> str:
    if not isinstance(body, dict):
        return ""
    msg = body.get("message")
    if isinstance(msg, dict):
        content = msg.get("content") or ""
        if content:
            return str(content)
    # A tool-invocation turn (e.g. a Home Assistant state query) leaves the
    # top-level `message.content` empty and carries the model's final summary in
    # the last assistant message of the `messages` array instead (#258).
    from_messages = _answer_from_messages(body.get("messages"))
    if from_messages:
        return from_messages
    return str(
        body.get("output")
        or body.get("reply")
        or body.get("response")
        or body.get("text")
        or ""
    )


def _answer_from_messages(messages: Any) -> str:
    """Last assistant `content` from a Hermes `messages` array, else "".

    Tool-invocation turns surface the model's final answer here rather than in
    `message.content` / streaming deltas, so both chat paths fall back to it
    (#258). The reasoning lives in a separate field and is skipped.
    """
    if not isinstance(messages, list):
        return ""
    for msg in reversed(messages):
        if not isinstance(msg, dict):
            continue
        if msg.get("role") not in (None, "assistant"):
            continue
        content = msg.get("content")
        if isinstance(content, list):
            content = "".join(
                str(p.get("text") or "") if isinstance(p, dict) else str(p)
                for p in content
            )
        if content:
            return str(content)
    return ""

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
from collections.abc import AsyncIterator
from typing import Any

import aiohttp

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

    async def create_session(self, uid: str, system_prompt: str | None = None) -> str:
        """Create a session bound to `uid`; return its id.

        `system_prompt` is the chosen personality's overlay (see
        `personalities.py`); Hermes accepts it only at create time (PATCH
        rejects it). Empty/None => no overlay, pure SOUL.md.
        """
        url = f"{self._base_url}/api/sessions"
        payload: dict[str, Any] = {"user_id": uid}
        if system_prompt:
            payload["system_prompt"] = system_prompt
        async with aiohttp.ClientSession(timeout=self._timeout) as client:
            async with client.post(url, json=payload, headers=self._headers()) as resp:
                body = await self._json_or_raise(resp, "create_session")
        session_id = _extract_session_id(body)
        if not session_id:
            raise HermesError("create_session: no session id in response")
        return session_id

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
        """List all sessions (single-resident: no per-resident filter).

        Per-resident isolation is deferred: Hermes v0.15.1 stores
        `user_id: null` (the `user_id` we POST on create is not persisted or
        returned), so an owner-filter here would reject every session
        including the caller's own. We ship list-all for the single-resident
        reality and restore filtering before multi-resident — tracked in #153.
        `uid` is still accepted so the signature survives that restore.
        Each item is `{id, title, last_activity}`.
        """
        url = f"{self._base_url}/api/sessions"
        async with aiohttp.ClientSession(timeout=self._timeout) as client:
            async with client.get(url, headers=self._headers()) as resp:
                body = await self._json_or_raise(resp, "list_sessions")
        return [_session_summary(raw) for raw in _iter_session_items(body)]

    async def get_session(self, session_id: str, uid: str) -> dict[str, Any] | None:
        """Fetch a session summary + its message history.

        The session endpoint returns only a summary; messages live on a
        separate `/messages` endpoint, so we fetch both.

        No ownership 404 (single-resident): Hermes v0.15.1 stores
        `user_id: null`, so an owner check would reject the caller's own
        session. Restore the per-resident gate before multi-resident — #153.
        Returns `None` only when Hermes itself 404s the id.
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
            msg_url = f"{self._base_url}/api/sessions/{session_id}/messages"
            async with client.get(msg_url, headers=self._headers()) as resp:
                msg_body = await self._json_or_raise(resp, "get_messages")
        summary = _session_summary(raw)
        summary["messages"] = _extract_messages(msg_body)
        return summary

    async def set_title(self, session_id: str, title: str) -> None:
        """Persist a session title (PATCH). Silent on a non-2xx — a title is
        a nicety, not worth failing the turn it rides on."""
        url = f"{self._base_url}/api/sessions/{session_id}"
        async with aiohttp.ClientSession(timeout=self._timeout) as client:
            async with client.patch(
                url, json={"title": title}, headers=self._headers()
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
        self, session_id: str, text: str, images: list[str] | None = None
    ) -> str:
        """Send one turn to an existing session; return the reply text.

        `images` are base64-encoded image payloads (camera/upload from the
        chat panel, #183); forwarded under the multimodal `images` key so the
        media-ingestion skill and a vision model can act on the attachment.
        """
        url = f"{self._base_url}/api/sessions/{session_id}/chat"
        async with aiohttp.ClientSession(timeout=self._timeout) as client:
            async with client.post(
                url, json=_chat_body(text, images), headers=self._headers()
            ) as resp:
                body = await self._json_or_raise(resp, "chat")
        return _extract_reply(body)

    async def chat_stream(
        self, session_id: str, text: str, images: list[str] | None = None
    ) -> AsyncIterator[dict[str, Any]]:
        """Stream one turn; yield parsed Hermes SSE events as dicts.

        Each yielded event is `{"type": <event>, "data": <decoded payload>}`.
        The `assistant.delta` event carries token deltas; `tool.started`/
        `tool.completed` carry tool names; `run.completed` ends the turn.
        `images` (base64, #183) ride the same multimodal `images` key.
        """
        url = f"{self._base_url}/api/sessions/{session_id}/chat/stream"
        async with aiohttp.ClientSession(timeout=self._timeout) as client:
            async with client.post(
                url, json=_chat_body(text, images), headers=self._headers()
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


def _chat_body(text: str, images: list[str] | None) -> dict[str, Any]:
    """Build the Hermes chat body, adding `images` only when present.

    `images` are bare base64 strings (no `data:` prefix), the multimodal
    convention Hermes/Ollama vision models read. Omitting the key entirely on
    a text-only turn keeps the request shape identical to before.
    """
    body: dict[str, Any] = {"input": text}
    if images:
        body["images"] = images
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
    return str(
        body.get("output")
        or body.get("reply")
        or body.get("response")
        or body.get("text")
        or ""
    )

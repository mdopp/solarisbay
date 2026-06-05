"""Thin client for Hermes' native session API.

Contract (NOT the gatekeeper's placeholder `/converse`):
  - `POST /api/sessions`                 -> create a session, returns `{"id": ...}`
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

from oscar_chat.logging import log


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

    async def create_session(self, uid: str) -> str:
        """Create a session bound to `uid`; return its id."""
        url = f"{self._base_url}/api/sessions"
        async with aiohttp.ClientSession(timeout=self._timeout) as client:
            async with client.post(
                url, json={"user_id": uid}, headers=self._headers()
            ) as resp:
                body = await self._json_or_raise(resp, "create_session")
        session_id = _extract_session_id(body)
        if not session_id:
            raise HermesError("create_session: no session id in response")
        return session_id

    async def chat(self, session_id: str, text: str) -> str:
        """Send one turn to an existing session; return the reply text."""
        url = f"{self._base_url}/api/sessions/{session_id}/chat"
        async with aiohttp.ClientSession(timeout=self._timeout) as client:
            async with client.post(
                url, json={"input": text}, headers=self._headers()
            ) as resp:
                body = await self._json_or_raise(resp, "chat")
        return _extract_reply(body)

    async def chat_stream(
        self, session_id: str, text: str
    ) -> AsyncIterator[dict[str, Any]]:
        """Stream one turn; yield parsed Hermes SSE events as dicts.

        Each yielded event is `{"type": <event>, "data": <decoded payload>}`.
        The `assistant.delta` event carries token deltas; `tool.started`/
        `tool.completed` carry tool names; `run.completed` ends the turn.
        """
        url = f"{self._base_url}/api/sessions/{session_id}/chat/stream"
        async with aiohttp.ClientSession(timeout=self._timeout) as client:
            async with client.post(
                url, json={"input": text}, headers=self._headers()
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

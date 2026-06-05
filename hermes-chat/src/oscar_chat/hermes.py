"""Thin client for Hermes' native session API.

Contract (NOT the gatekeeper's placeholder `/converse`):
  - `POST /api/sessions`            -> create a session, returns `{"id": ...}`
  - `POST /api/sessions/{id}/chat`  -> body `{"input": ...}`, returns the reply

Auth is a bearer token (`API_SERVER_KEY`) held server-side; the browser
never sees it.
"""

from __future__ import annotations

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

    @staticmethod
    async def _json_or_raise(resp: aiohttp.ClientResponse, op: str) -> Any:
        if resp.status >= 400:
            detail = (await resp.text())[:500]
            log.error("chat.hermes.error", op=op, status=resp.status, body=detail)
            raise HermesError(f"{op}: Hermes returned {resp.status}")
        return await resp.json()


def _extract_session_id(body: Any) -> str:
    if not isinstance(body, dict):
        return ""
    # Tolerate a couple of envelope shapes Hermes may use.
    session = body.get("session") if isinstance(body.get("session"), dict) else body
    return str(session.get("id") or session.get("session_id") or "")


def _extract_reply(body: Any) -> str:
    if not isinstance(body, dict):
        return ""
    return str(
        body.get("output")
        or body.get("reply")
        or body.get("response")
        or body.get("text")
        or ""
    )

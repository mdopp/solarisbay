"""Client for Hermes' native session API.

Contract (the real Hermes API, port 8642, Bearer `API_SERVER_KEY`):
  - `POST /api/sessions`            -> create a session, returns `{"id": ...}`
  - `POST /api/sessions/{id}/chat`  -> body `{"input": ...}`, reply in
    `{"message": {"content": ...}}`

The gatekeeper owns one Hermes session per conversation (keyed by the uid or
the originating satellite) for real continuity across turns; an expired
session (404) is recreated once and the turn retried.

Adaptive routing (#187): when `fast_model` is set, the client keeps a second
"fast" session per conversation (keyed `<conv>:fast`, created with that model
override) and tries it first. Hermes session chat returns only plain text —
no tool-call or confidence signal reaches the gatekeeper — so the only
observable quality signal is the reply itself: an empty reply, or one shorter
than a short-confirmation that doesn't look like a confirmation, is treated as
a fast-model miss and the same turn is replayed on the normal (slow) session.
When `fast_model` is empty the fast path is a no-op: single-session behaviour,
unchanged.
"""

from __future__ import annotations

import re
from typing import Any

import httpx
from gatekeeper import marker
from gatekeeper.logging import log

# A "good" short reply is usually a confirmation ("ok", "done", "sure",
# "alles klar"). Anything shorter than this that *isn't* such a phrase is
# treated as a fast-model miss worth escalating.
_MIN_GOOD_REPLY_LEN = 20
_CONFIRMATION_RE = re.compile(
    r"\b("
    r"ok|okay|okey|done|sure|yes|no|yep|nope|got it|on it|"
    r"ja|nein|klar|alles klar|gerne|erledigt|mach ich|fertig"
    r")\b",
    re.IGNORECASE,
)


def _is_low_quality_reply(reply: str) -> bool:
    """Heuristic fast-model miss: empty, or short without a confirmation."""
    stripped = reply.strip()
    if not stripped:
        return True
    if len(stripped) >= _MIN_GOOD_REPLY_LEN:
        return False
    return _CONFIRMATION_RE.search(stripped) is None


class HermesClient:
    def __init__(
        self,
        base_url: str,
        token: str,
        timeout: float = 30.0,
        fast_model: str = "",
    ):
        self._base_url = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout
        self._fast_model = fast_model.strip()
        self._sessions: dict[str, str] = {}

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    async def converse(
        self,
        *,
        text: str,
        uid: str,
        endpoint: str,
        trace_id: str,
        location: str | None = None,
    ) -> str:
        conv_key = uid or endpoint
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            if self._fast_model:
                fast_reply = await self._turn(
                    client,
                    conv_key=f"{conv_key}:fast",
                    uid=uid,
                    text=text,
                    trace_id=trace_id,
                    model=self._fast_model,
                )
                if not _is_low_quality_reply(fast_reply):
                    return fast_reply
                log.info(
                    "gatekeeper.hermes.fast_fallback",
                    trace_id=trace_id,
                    fast_len=len(fast_reply.strip()),
                )

            return await self._turn(
                client,
                conv_key=conv_key,
                uid=uid,
                text=text,
                trace_id=trace_id,
                model=None,
            )

    async def _turn(
        self,
        client: httpx.AsyncClient,
        *,
        conv_key: str,
        uid: str,
        text: str,
        trace_id: str,
        model: str | None,
    ) -> str:
        session_id = self._sessions.get(conv_key)
        if session_id is None:
            session_id = await self._create_session(client, uid, trace_id, model)
            if not session_id:
                return ""
            self._sessions[conv_key] = session_id

        response = await self._chat(client, session_id, text)
        if response is not None and response.status_code == 404:
            # Session expired upstream — recreate once and retry the turn.
            session_id = await self._create_session(client, uid, trace_id, model)
            if not session_id:
                self._sessions.pop(conv_key, None)
                return ""
            self._sessions[conv_key] = session_id
            response = await self._chat(client, session_id, text)

        if response is None:
            return ""
        if response.status_code >= 400:
            log.error(
                "gatekeeper.hermes.error",
                trace_id=trace_id,
                status=response.status_code,
                body=response.text[:500],
            )
            return ""
        return _extract_reply(response.json())

    async def _create_session(
        self,
        client: httpx.AsyncClient,
        uid: str,
        trace_id: str,
        model: str | None = None,
    ) -> str:
        url = f"{self._base_url}/api/sessions"
        # Seed the title with the resident's immutable uid marker (#153) so the
        # chat panel's per-resident list filter sees voice sessions too. uid is
        # 'household' for all voice turns until speaker-ID (#84) is enabled, so
        # voice isolation is single-user only in the current config.
        payload: dict[str, Any] = {"user_id": uid, "title": marker.marker_for(uid)}
        if model:
            payload["model"] = model
        response = await client.post(url, json=payload, headers=self._headers())
        if response.status_code >= 400:
            log.error(
                "gatekeeper.hermes.error",
                trace_id=trace_id,
                status=response.status_code,
                body=response.text[:500],
            )
            return ""
        return _extract_session_id(response.json())

    async def _chat(
        self, client: httpx.AsyncClient, session_id: str, text: str
    ) -> httpx.Response:
        url = f"{self._base_url}/api/sessions/{session_id}/chat"
        return await client.post(url, json={"input": text}, headers=self._headers())


def _extract_session_id(body: Any) -> str:
    if not isinstance(body, dict):
        return ""
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

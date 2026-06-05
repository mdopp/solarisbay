"""aiohttp app: serve the static chat page and proxy turns to Hermes.

Stateless by design — the server holds no chat/session store. The browser
keeps the current session id and sends it back with each turn; on the first
turn (no id) the server creates a session bound to the SSO identity and
returns the id. All chat/session state lives in Hermes (`~/.hermes`).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from aiohttp import web

from oscar_chat.hermes import HermesClient, HermesError
from oscar_chat.logging import log

STATIC_DIR = Path(__file__).parent / "static"


def resolve_uid(request: web.Request, header: str, default_uid: str) -> str:
    """Map the Authelia trusted-proxy identity header to a Hermes uid.

    NPM sets `Remote-User` after Authelia authenticates; we fold that into
    the Hermes uid so there is no second login. Absent header (e.g. direct
    loopback access for offline testing) falls back to `default_uid`.
    """
    value = request.headers.get(header, "").strip()
    return value or default_uid


def build_app(
    *,
    hermes: HermesClient,
    remote_user_header: str,
    default_uid: str,
) -> web.Application:
    async def index(_request: web.Request) -> web.Response:
        return web.FileResponse(STATIC_DIR / "index.html")

    async def health(_request: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    async def list_sessions(request: web.Request) -> web.Response:
        uid = resolve_uid(request, remote_user_header, default_uid)
        try:
            sessions = await hermes.list_sessions(uid)
        except HermesError:
            return web.json_response(
                {"ok": False, "reason": "hermes_unavailable"}, status=502
            )
        return web.json_response({"ok": True, "sessions": sessions})

    async def create_session(request: web.Request) -> web.Response:
        uid = resolve_uid(request, remote_user_header, default_uid)
        try:
            session_id = await hermes.create_session(uid)
        except HermesError:
            return web.json_response(
                {"ok": False, "reason": "hermes_unavailable"}, status=502
            )
        log.info("chat.session.created", uid=uid, session_id=session_id)
        return web.json_response({"ok": True, "session_id": session_id})

    async def get_session(request: web.Request) -> web.Response:
        uid = resolve_uid(request, remote_user_header, default_uid)
        session_id = request.match_info["session_id"]
        try:
            session = await hermes.get_session(session_id, uid)
        except HermesError:
            return web.json_response(
                {"ok": False, "reason": "hermes_unavailable"}, status=502
            )
        if session is None:
            return web.json_response({"ok": False, "reason": "not_found"}, status=404)
        return web.json_response({"ok": True, "session": session})

    async def chat(request: web.Request) -> web.Response:
        uid = resolve_uid(request, remote_user_header, default_uid)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 — any malformed JSON
            return web.json_response(
                {"ok": False, "reason": "invalid_json"}, status=400
            )

        text = str(body.get("input") or "").strip()
        if not text:
            return web.json_response({"ok": False, "reason": "empty_input"}, status=400)
        session_id = str(body.get("session_id") or "")

        try:
            if not session_id:
                session_id = await hermes.create_session(uid)
                log.info("chat.session.created", uid=uid, session_id=session_id)
            reply = await hermes.chat(session_id, text)
        except HermesError:
            return web.json_response(
                {"ok": False, "reason": "hermes_unavailable"}, status=502
            )

        return web.json_response({"ok": True, "session_id": session_id, "reply": reply})

    async def chat_stream(request: web.Request) -> web.StreamResponse:
        uid = resolve_uid(request, remote_user_header, default_uid)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 — any malformed JSON
            return web.json_response(
                {"ok": False, "reason": "invalid_json"}, status=400
            )

        text = str(body.get("input") or "").strip()
        if not text:
            return web.json_response({"ok": False, "reason": "empty_input"}, status=400)
        session_id = str(body.get("session_id") or "")

        resp = web.StreamResponse(
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            }
        )
        await resp.prepare(request)

        try:
            if not session_id:
                session_id = await hermes.create_session(uid)
                log.info("chat.session.created", uid=uid, session_id=session_id)
            await _send_event(resp, "session", {"session_id": session_id})
            async for event in hermes.chat_stream(session_id, text):
                await _send_event(resp, *_normalize(event))
        except HermesError:
            await _send_event(resp, "error", {"reason": "hermes_unavailable"})
        await _send_event(resp, "done", {})
        return resp

    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/health", health)
    app.router.add_get("/api/sessions", list_sessions)
    app.router.add_post("/api/sessions", create_session)
    app.router.add_get("/api/sessions/{session_id}", get_session)
    app.router.add_post("/api/chat", chat)
    app.router.add_post("/api/chat/stream", chat_stream)
    app.router.add_static("/static/", STATIC_DIR)
    return app


async def _send_event(
    resp: web.StreamResponse, event: str, data: dict[str, Any]
) -> None:
    frame = f"event: {event}\ndata: {json.dumps(data)}\n\n"
    await resp.write(frame.encode("utf-8"))


def _normalize(event: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Fold a Hermes SSE event into a browser-facing `(event, data)` pair.

    The browser only needs four shapes: a token delta, a tool start/stop
    hint, and an end marker. Anything else collapses to a no-op `keepalive`.
    """
    etype = str(event.get("type") or "")
    data = event.get("data")
    payload = data if isinstance(data, dict) else {}
    if etype == "assistant.delta":
        text = payload.get("delta") or payload.get("text") or payload.get("content")
        if not text and isinstance(data, str):
            text = data
        return "delta", {"text": str(text or "")}
    if etype in ("tool.started", "tool.completed"):
        name = payload.get("tool") or payload.get("name") or ""
        phase = "started" if etype == "tool.started" else "completed"
        return "tool", {"name": str(name), "phase": phase}
    if etype == "run.completed":
        return "completed", {}
    return "keepalive", {}


async def serve(
    host: str,
    port: int,
    *,
    hermes: HermesClient,
    remote_user_header: str,
    default_uid: str,
) -> None:
    app = build_app(
        hermes=hermes,
        remote_user_header=remote_user_header,
        default_uid=default_uid,
    )
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    log.info("chat.listening", host=host, port=port)
    try:
        import asyncio

        await asyncio.Event().wait()
    finally:
        await runner.cleanup()

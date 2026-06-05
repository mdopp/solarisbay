"""aiohttp app: serve the static chat page and proxy turns to Hermes.

Stateless by design — the server holds no chat/session store. The browser
keeps the current session id and sends it back with each turn; on the first
turn (no id) the server creates a session bound to the SSO identity and
returns the id. All chat/session state lives in Hermes (`~/.hermes`).
"""

from __future__ import annotations

from pathlib import Path

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

    app = web.Application()
    app.router.add_get("/", index)
    app.router.add_get("/health", health)
    app.router.add_post("/api/chat", chat)
    app.router.add_static("/static/", STATIC_DIR)
    return app


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

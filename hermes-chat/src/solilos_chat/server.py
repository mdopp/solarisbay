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

import aiohttp
from aiohttp import web

from solilos_chat import personalities, skills
from solilos_chat.hermes import HermesClient, HermesError
from solilos_chat.logging import log

STATIC_DIR = Path(__file__).parent / "static"


def _version() -> str:
    """The chat package version, for the sidebar footer. '' if unavailable."""
    try:
        from importlib.metadata import version

        return version("solilos-chat")
    except Exception:  # noqa: BLE001 — metadata absent in some run contexts
        return ""


VERSION = _version()


def _title_from(text: str) -> str:
    """Derive a short session title from the first user message.

    Hermes leaves chat-created sessions title-null; we PATCH this in so the
    list shows a meaningful label instead of a placeholder for every row.
    """
    snippet = " ".join(text.split())
    return snippet[:57].rstrip() + "…" if len(snippet) > 60 else snippet


def resolve_uid(request: web.Request, header: str, default_uid: str) -> str:
    """Map the Authelia trusted-proxy identity header to a Hermes uid.

    NPM sets `Remote-User` after Authelia authenticates; we fold that into
    the Hermes uid so there is no second login. Absent header (e.g. direct
    loopback access for offline testing) falls back to `default_uid`.
    """
    value = request.headers.get(header, "").strip()
    return value or default_uid


def is_admin(request: web.Request, header: str, admin_group: str) -> bool:
    """True when the Authelia groups header lists `admin_group`.

    Authelia forwards `Remote-Groups` as a comma-separated list through the
    trusted proxy. Panel writes (phase 2) gate on this; phase-1 reads use it
    only to tell the browser which controls to surface.
    """
    raw = request.headers.get(header, "")
    groups = {g.strip() for g in raw.split(",") if g.strip()}
    return admin_group in groups


def build_app(
    *,
    hermes: HermesClient,
    remote_user_header: str,
    default_uid: str,
    remote_groups_header: str = "Remote-Groups",
    admin_group: str = "admins",
    skills_dir: str = "/data/skills",
    soul_path: str = "/data/SOUL.md",
    config_agent_url: str = "http://127.0.0.1:8650",
    agent_token: str = "",
    logout_url: str = "",
    context_window: int = 131072,
) -> web.Application:
    async def index(_request: web.Request) -> web.Response:
        return web.FileResponse(STATIC_DIR / "index.html")

    async def health(_request: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    async def whoami(request: web.Request) -> web.Response:
        return web.json_response(
            {
                "ok": True,
                "uid": resolve_uid(request, remote_user_header, default_uid),
                "is_admin": is_admin(request, remote_groups_header, admin_group),
                "version": VERSION,
                "logout_url": logout_url,
                "context_window": context_window,
            }
        )

    async def list_toolsets(_request: web.Request) -> web.Response:
        try:
            toolsets = await hermes.list_toolsets()
        except HermesError:
            return web.json_response(
                {"ok": False, "reason": "hermes_unavailable"}, status=502
            )
        return web.json_response({"ok": True, "toolsets": toolsets})

    async def list_personalities(_request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "personalities": personalities.catalog()})

    async def list_skills(_request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "skills": skills.list_skills(skills_dir)})

    async def get_skill(request: web.Request) -> web.Response:
        skill = skills.read_skill(skills_dir, request.match_info["skill_id"])
        if skill is None:
            return web.json_response({"ok": False, "reason": "not_found"}, status=404)
        return web.json_response({"ok": True, "skill": skill})

    async def put_skill(request: web.Request) -> web.Response:
        if not is_admin(request, remote_groups_header, admin_group):
            return web.json_response({"ok": False, "reason": "forbidden"}, status=403)
        skill_id = request.match_info["skill_id"]
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 — any malformed JSON
            return web.json_response(
                {"ok": False, "reason": "invalid_json"}, status=400
            )
        content = body.get("content")
        if not isinstance(content, str) or not content.strip():
            return web.json_response(
                {"ok": False, "reason": "empty_content"}, status=400
            )
        try:
            result = skills.write_skill(skills_dir, skill_id, content)
        except OSError:
            return web.json_response(
                {"ok": False, "reason": "write_failed"}, status=500
            )
        if result is None:
            return web.json_response({"ok": False, "reason": "not_found"}, status=404)
        log.info(
            "chat.skill.edited",
            uid=resolve_uid(request, remote_user_header, default_uid),
            skill=skill_id,
            frontmatter_changed=result["frontmatter_changed"],
        )
        return web.json_response(
            {"ok": True, "restart_needed": result["frontmatter_changed"]}
        )

    async def get_soul(_request: web.Request) -> web.Response:
        # Read through the sidecar (single source of truth), not a local
        # mount: the agent's atomic writes swap the file inode, which a
        # single-file bind mount in this pod wouldn't track (stale reads).
        content = await _agent_get_soul(config_agent_url, agent_token)
        if content is None:
            return web.json_response(
                {"ok": False, "reason": "agent_unavailable"}, status=502
            )
        return web.json_response({"ok": True, "soul": {"content": content}})

    async def put_soul(request: web.Request) -> web.Response:
        if not is_admin(request, remote_groups_header, admin_group):
            return web.json_response({"ok": False, "reason": "forbidden"}, status=403)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 — any malformed JSON
            return web.json_response(
                {"ok": False, "reason": "invalid_json"}, status=400
            )
        content = body.get("content")
        if not isinstance(content, str) or not content.strip():
            return web.json_response(
                {"ok": False, "reason": "empty_content"}, status=400
            )
        # The chat pod can't write Hermes' data dir; the privileged sidecar
        # in the hermes pod does it. SOUL.md reloads live, so no restart.
        ok = await _agent_put_soul(config_agent_url, agent_token, content)
        if not ok:
            return web.json_response(
                {"ok": False, "reason": "agent_unavailable"}, status=502
            )
        log.info(
            "chat.soul.edited",
            uid=resolve_uid(request, remote_user_header, default_uid),
        )
        return web.json_response({"ok": True})

    async def get_model(request: web.Request) -> web.Response:
        # Admin-only: the switch is an admin control and listing models is
        # part of it; the panel only shows this to admins.
        if not is_admin(request, remote_groups_header, admin_group):
            return web.json_response({"ok": False, "reason": "forbidden"}, status=403)
        data = await _agent_get_model(config_agent_url, agent_token)
        if data is None:
            return web.json_response(
                {"ok": False, "reason": "agent_unavailable"}, status=502
            )
        return web.json_response(
            {
                "ok": True,
                "current": data.get("current", ""),
                "available": data.get("available", []),
            }
        )

    async def put_model(request: web.Request) -> web.Response:
        if not is_admin(request, remote_groups_header, admin_group):
            return web.json_response({"ok": False, "reason": "forbidden"}, status=403)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 — any malformed JSON
            return web.json_response(
                {"ok": False, "reason": "invalid_json"}, status=400
            )
        model = body.get("model")
        if not isinstance(model, str) or not model.strip():
            return web.json_response({"ok": False, "reason": "empty_model"}, status=400)
        # Persistent change: the sidecar rewrites config.yaml and restarts
        # Hermes (so it's admin-gated, per the panel's access model).
        result = await _agent_put_model(config_agent_url, agent_token, model.strip())
        if result is None:
            return web.json_response(
                {"ok": False, "reason": "agent_unavailable"}, status=502
            )
        log.info(
            "chat.model.set",
            uid=resolve_uid(request, remote_user_header, default_uid),
            model=model.strip(),
        )
        return web.json_response(
            {"ok": True, "restarted": bool(result.get("restarted"))}
        )

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
        personality_id = await _personality_from(request)
        system_prompt = personalities.system_prompt_for(personality_id)
        try:
            session_id = await hermes.create_session(uid, system_prompt)
        except HermesError:
            return web.json_response(
                {"ok": False, "reason": "hermes_unavailable"}, status=502
            )
        log.info(
            "chat.session.created",
            uid=uid,
            session_id=session_id,
            personality=personality_id or "",
        )
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

    async def delete_session(request: web.Request) -> web.Response:
        # No ownership gate (single-resident reality — list-all/open-any until
        # per-resident isolation, #153). Deleting a session just removes it
        # from the shared household list.
        session_id = request.match_info["session_id"]
        try:
            ok = await hermes.delete_session(session_id)
        except HermesError:
            return web.json_response(
                {"ok": False, "reason": "hermes_unavailable"}, status=502
            )
        if not ok:
            return web.json_response(
                {"ok": False, "reason": "delete_failed"}, status=502
            )
        log.info(
            "chat.session.deleted",
            uid=resolve_uid(request, remote_user_header, default_uid),
            session_id=session_id,
        )
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
        system_prompt = personalities.system_prompt_for(body.get("personality"))

        try:
            if not session_id:
                session_id = await hermes.create_session(uid, system_prompt)
                log.info("chat.session.created", uid=uid, session_id=session_id)
                await hermes.set_title(session_id, _title_from(text))
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
        system_prompt = personalities.system_prompt_for(body.get("personality"))

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
                session_id = await hermes.create_session(uid, system_prompt)
                log.info("chat.session.created", uid=uid, session_id=session_id)
                await hermes.set_title(session_id, _title_from(text))
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
    app.router.add_get("/api/whoami", whoami)
    app.router.add_get("/api/toolsets", list_toolsets)
    app.router.add_get("/api/personalities", list_personalities)
    app.router.add_get("/api/skills", list_skills)
    app.router.add_get("/api/skills/{skill_id}", get_skill)
    app.router.add_put("/api/skills/{skill_id}", put_skill)
    app.router.add_get("/api/soul", get_soul)
    app.router.add_put("/api/soul", put_soul)
    app.router.add_get("/api/model", get_model)
    app.router.add_put("/api/model", put_model)
    app.router.add_get("/api/sessions", list_sessions)
    app.router.add_post("/api/sessions", create_session)
    app.router.add_get("/api/sessions/{session_id}", get_session)
    app.router.add_delete("/api/sessions/{session_id}", delete_session)
    app.router.add_post("/api/chat", chat)
    app.router.add_post("/api/chat/stream", chat_stream)
    app.router.add_static("/static/", STATIC_DIR)
    return app


async def _personality_from(request: web.Request) -> str | None:
    """Pull an optional `personality` id from a JSON body (POST create).

    Create is a body-less POST in the normal flow, so a missing/!JSON body
    is fine — it just means the default personality.
    """
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 — body-less or malformed = default
        return None
    return body.get("personality") if isinstance(body, dict) else None


def _agent_headers(token: str) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


async def _agent_put_soul(agent_url: str, token: str, content: str) -> bool:
    """Ask the hermes-pod config sidecar to write SOUL.md. True on 2xx."""
    url = f"{agent_url.rstrip('/')}/soul"
    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as client:
            async with client.put(
                url, json={"content": content}, headers=_agent_headers(token)
            ) as r:
                if r.status < 400:
                    return True
                detail = (await r.text())[:300]
                log.error(
                    "chat.agent.error", op="put_soul", status=r.status, body=detail
                )
                return False
    except (aiohttp.ClientError, TimeoutError, OSError) as e:
        log.error("chat.agent.unreachable", op="put_soul", error=str(e))
        return False


async def _agent_get_soul(agent_url: str, token: str) -> str | None:
    """Read SOUL.md content from the sidecar. None on failure."""
    url = f"{agent_url.rstrip('/')}/soul"
    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as client:
            async with client.get(url, headers=_agent_headers(token)) as r:
                if r.status >= 400:
                    return None
                body = await r.json()
        return str(body.get("content", "")) if isinstance(body, dict) else None
    except (aiohttp.ClientError, TimeoutError, OSError, ValueError) as e:
        log.error("chat.agent.unreachable", op="get_soul", error=str(e))
        return None


async def _agent_get_model(agent_url: str, token: str) -> dict[str, Any] | None:
    """Read {current, available} from the sidecar. None on failure."""
    url = f"{agent_url.rstrip('/')}/model"
    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as client:
            async with client.get(url, headers=_agent_headers(token)) as r:
                if r.status >= 400:
                    return None
                return await r.json()
    except (aiohttp.ClientError, TimeoutError, OSError, ValueError) as e:
        log.error("chat.agent.unreachable", op="get_model", error=str(e))
        return None


async def _agent_put_model(
    agent_url: str, token: str, model: str
) -> dict[str, Any] | None:
    """Ask the sidecar to set the model (it restarts Hermes). The agent's
    JSON ({ok, restarted}) on 2xx, None otherwise."""
    url = f"{agent_url.rstrip('/')}/model"
    try:
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as client:
            async with client.put(
                url, json={"model": model}, headers=_agent_headers(token)
            ) as r:
                if r.status >= 400:
                    detail = (await r.text())[:300]
                    log.error(
                        "chat.agent.error", op="put_model", status=r.status, body=detail
                    )
                    return None
                return await r.json()
    except (aiohttp.ClientError, TimeoutError, OSError, ValueError) as e:
        log.error("chat.agent.unreachable", op="put_model", error=str(e))
        return None


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
    remote_groups_header: str = "Remote-Groups",
    admin_group: str = "admins",
    skills_dir: str = "/data/skills",
    soul_path: str = "/data/SOUL.md",
    config_agent_url: str = "http://127.0.0.1:8650",
    agent_token: str = "",
    logout_url: str = "",
    context_window: int = 131072,
) -> None:
    app = build_app(
        hermes=hermes,
        remote_user_header=remote_user_header,
        default_uid=default_uid,
        remote_groups_header=remote_groups_header,
        admin_group=admin_group,
        skills_dir=skills_dir,
        soul_path=soul_path,
        config_agent_url=config_agent_url,
        agent_token=agent_token,
        logout_url=logout_url,
        context_window=context_window,
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

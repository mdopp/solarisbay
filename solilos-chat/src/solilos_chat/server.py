"""aiohttp app: serve the static chat page and proxy turns to Hermes.

Stateless by design — the server holds no chat/session store. The browser
keeps the current session id and sends it back with each turn; on the first
turn (no id) the server creates a session bound to the SSO identity and
returns the id. All chat/session state lives in Hermes (`~/.hermes`).
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import aiohttp
from aiohttp import web

from solilos_chat import (
    compaction,
    mentions_store,
    notes_search,
    personalities,
    reasoning,
    skills,
    topics_store,
    trace_store,
)
from solilos_chat.attachments import AttachmentStore, attach_to_messages
from solilos_chat.context import STATIC_DEFAULT, ContextWindow
from solilos_chat.hermes import HermesClient, HermesError, _answer_from_messages
from solilos_chat.logging import log

STATIC_DIR = Path(__file__).parent / "static"

# Default prompt for an image-only turn (attachment with no typed text), so the
# media-ingestion skill has a turn to trigger on. Mirrors the German tone the
# skill itself uses with residents.
_IMAGE_PROMPT = "Bitte sieh dir dieses Bild an und verarbeite es."
# Cap attachments per turn — a small guard against an oversized payload, not a
# product limit (the panel sends at most a couple of camera/upload images).
_MAX_IMAGES = 4

# Ephemeral/incognito chats (#246): the proxy prepends this to every turn so the
# agent knows nothing is durable — it must NOT auto-ingest notes, write memory
# facts, or otherwise persist anything unless the resident explicitly asks to
# extract a note (which carries a topic and routes through `topic_turn_text`).
_EPHEMERAL_HINT = (
    "[Temporary/incognito chat: this conversation is ephemeral and will be "
    "deleted on close. Do NOT save notes, store memory facts, or persist "
    "anything from it. Persist ONLY if the resident explicitly asks to extract "
    "a note (e.g. 'erstelle hieraus eine Notiz im Topic X').]"
)


_LOCAL_TZ = ZoneInfo("Europe/Berlin")


def _now_hint() -> str:
    """A fresh local wall-clock line prepended to every user turn.

    Hermes stamps the session with a frozen, date-granular "Conversation
    started" line at create time and replays it verbatim; the container also
    runs UTC. Without a per-turn line the agent reports a wrong/frozen
    date-time. Hermes binds system_prompt at create and rejects per-turn
    updates, so injecting this into the user turn is the only lever.
    """
    now = datetime.now(_LOCAL_TZ)
    return f"[Aktuelle Zeit: {now.strftime('%A, %d.%m.%Y, %H:%M Uhr %Z')}]"


# Leading internal-hint prefixes the proxy/gatekeeper inject into a user turn so
# the agent reads context the resident never typed (#309). They must NOT appear
# in the rendered history; this matches what's actually injected:
#   server.topic_turn_text  -> "[Aktuelle Zeit: ...]", "[Active topic: ... #topic/<slug>]",
#                              the "[Temporary/incognito ...]" ephemeral guard,
#                              "[Extract this to a note #topic/<slug> (...)]"
#   voice gatekeeper.hermes -> "[room: <location>]" (#312/#313)
# Each rides as a leading bracketed block; topic_turn_text joins them with "\n\n",
# the voice room hint with "\n". `[uid:...]` lives on the title (marker.py), but a
# leading one is stripped too for safety. Only LEADING hints are removed so a hint
# the resident actually typed mid-message survives.
_HINT_PREFIX_RE = re.compile(
    r"^\[(?:Aktuelle Zeit:|Temporary/incognito|Active topic:|Extract this to a note|room:|uid:)[^\]]*\]\s*",
    re.IGNORECASE,
)


def strip_internal_hints(content: str) -> str:
    """Drop leading internal-hint prefixes from a user message for DISPLAY (#309).

    Display-only: what was sent to Hermes is unchanged — this runs on the way out
    of the messages API. Strips each consecutive leading bracketed hint block,
    then the whitespace it was joined with, leaving the resident's actual text.
    """
    prev = None
    while content != prev:
        prev = content
        content = _HINT_PREFIX_RE.sub("", content, count=1)
    return content


def _version() -> str:
    """The Solilos release version, for the sidebar footer. '' if unavailable.

    Prefers the `SOLILOS_VERSION` env injected at image build (the release
    git tag/ref, see build-images.yml) — the package version in pyproject.toml
    is never bumped (releases are git tags, no release-please), so it would
    always read "0.1.0". Falls back to the package metadata for local/dev
    builds where the env is unset, so the badge still shows something.
    """
    import os

    env = os.environ.get("SOLILOS_VERSION", "").strip()
    if env:
        return env
    try:
        from importlib.metadata import version

        return version("solilos-chat")
    except Exception:  # noqa: BLE001 — metadata absent in some run contexts
        return ""


VERSION = _version()


# Inline mention tokens (#279): a word-boundary `#`/`@` followed by a run of
# tag-safe characters (letters/digits/_-, plus the topic-hierarchy `/`). The
# negative lookbehind keeps mid-word `#`/`@` (e.g. an email's `@`, a `C#`)
# from matching; the captured group excludes the marker char.
_TAG_RE = re.compile(r"(?<![\w/])#([\w/-]+)", re.UNICODE)
_PERSON_RE = re.compile(r"(?<![\w/])@([\w/-]+)", re.UNICODE)


def parse_mentions(text: str) -> tuple[list[str], list[str]]:
    """Split `#tag` / `@person` tokens out of a turn's text.

    Returns `(tags, persons)`, each de-duplicated, lower-cased, order-preserved.
    The leading `#`/`@` is dropped; the bare value is what's stored/suggested.
    """
    tags = _dedup(m.lower() for m in _TAG_RE.findall(text))
    persons = _dedup(m.lower() for m in _PERSON_RE.findall(text))
    return tags, persons


def _dedup(values: Any) -> list[str]:
    seen: dict[str, None] = {}
    for v in values:
        if v:
            seen.setdefault(v, None)
    return list(seen)


# Manual person seed for `@person` autosuggest before a resident has used any
# name in chat (#279). Residents/uids contribute the rest at runtime. CardDAV
# enrichment (#207, parked behind gbrain) is a future source that appends here —
# `seeded_persons()` is the single seam it plugs into. Keep this list small.
_MANUAL_PERSONS = ["mdopp", "anna", "lena"]


def seeded_persons(residents: Any) -> list[str]:
    """The person-suggestion seed: known residents/uids + a manual list.

    De-duplicated, lower-cased. A CardDAV source (#207) would extend this by
    unioning its contact names in here — the autosuggest endpoint reads only
    this function, so adding a source is a one-place change.
    """
    return _dedup(p.lower() for p in [*(residents or []), *_MANUAL_PERSONS])


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
    hermes_admin: HermesClient | None = None,
    remote_user_header: str,
    default_uid: str,
    remote_groups_header: str = "Remote-Groups",
    admin_group: str = "admins",
    skills_dir: str = "/data/skills",
    soul_path: str = "/data/SOUL.md",
    config_agent_url: str = "http://127.0.0.1:8650",
    agent_token: str = "",
    logout_url: str = "",
    context_window: ContextWindow | int = STATIC_DEFAULT,
    compaction_threshold: float = compaction.DEFAULT_THRESHOLD,
    attachments_dir: str = "/data/attachments",
    frame_ancestors: str = "'self'",
    fast_model: str = "",
    thorough_model: str = "",
    solilos_db_path: str = "/var/lib/solilos/solilos.db",
    notes_dir: str = "/opt/data/notes",
    trace_proxy_url: str = "http://127.0.0.1:11436",
    residents: list[str] | None = None,
) -> web.Application:
    # Known resident uids feeding the `@person` autosuggest seed (#279), beyond
    # the manual list in seeded_persons. The caller's own uid is always folded
    # in at the endpoint, so this is the *other* residents.
    resident_uids = list(residents or [])
    if isinstance(context_window, int):
        context_window = ContextWindow.static(context_window)
    # Hermes drops inbound images (persists a `[screenshot]` placeholder, no
    # attachment API), so the proxy persists the sent data URLs itself and
    # re-attaches them on history load (#202) — the one stateful exception.
    attachments = AttachmentStore(attachments_dir)

    # Active streaming turns, keyed by session id (#192). Each entry is an
    # asyncio.Event the stream loop polls; POST /api/chat/cancel sets it, which
    # breaks the loop and closes the upstream Hermes connection (closing that
    # connection is what actually interrupts the model's generation).
    cancels: dict[str, asyncio.Event] = {}

    # Multi-profile routing (#293): household sessions ride `hermes` (the
    # household gateway, :8642); admin/servicebay-maintenance sessions ride the
    # admin gateway (:8643). A session created on the admin gateway is recorded
    # here so its follow-up turns route back to the same gateway — Hermes session
    # state is per-gateway, so a session must stay on the instance that holds it.
    # When no admin gateway is configured both fall back to `hermes` (single-
    # instance / offline-test topology).
    household_gw = hermes
    admin_gw = hermes_admin or hermes
    admin_sessions: set[str] = set()

    def gateway_for(
        request: web.Request, session_id: str, persona: object = None
    ) -> HermesClient:
        """Pick the Hermes gateway for a turn (#293).

        Admin gateway only when the caller is an Authelia admin AND either the
        session was created on the admin gateway (recorded at create) or this
        request explicitly selects the admin/maintenance persona. A non-admin
        caller is ALWAYS routed to household — even if it presents a known admin
        session_id — so the #209/#229 gate holds at the routing layer too.
        """
        if not is_admin(request, remote_groups_header, admin_group):
            return household_gw
        if session_id and session_id in admin_sessions:
            return admin_gw
        sel = request.rel_url.query.get("persona") or persona
        if sel == personalities.MAINTENANCE_ID:
            return admin_gw
        return household_gw

    async def maybe_compact(
        uid: str, session_id: str, client: HermesClient
    ) -> tuple[str, bool]:
        """Hard-cap trigger (#210): if an existing session's running token usage
        is near the context-window cap, extract durable learnings to memory and
        compact into a continuation session *before* the next turn runs.

        Returns `(session_id, compacted)` — the continuation id when compaction
        happened, else the original id unchanged. Failure to compact degrades to
        "use the original session" (compact_session returns None), so a turn is
        never lost or blocked by compaction.

        No base_system_prompt is passed (#293): the gateway's profile supplies
        the soul, so the continuation session inherits it without a per-session
        overlay (default `base_system_prompt=""`).
        """
        try:
            new_id = await compaction.compact_session(
                client,
                uid,
                session_id,
                context_window=context_window.value,
                threshold=compaction_threshold,
            )
        except HermesError:
            return session_id, False
        if new_id:
            return new_id, True
        return session_id, False

    def new_session_topic(topic_slug: str) -> str | None:
        """The primary topic to persist for a new session, or None (#241/#242).

        The household gateway's profile (#293) now OWNS the soul and the base
        model, so a session no longer carries a per-session persona overlay or a
        model override at create — those would fight the profile. What survives
        is the topic binding: a chat started under a topic is tagged with it (the
        picker selects one before the first message, or a pinned topic-chat #237
        starts pre-assigned) so its turns get the #243 topic context hint and its
        ingested notes are stamped `#topic/<slug>`. Returns the slug to persist
        as primary, or None when no topic was supplied.
        """
        return topic_slug or None

    async def create_turn_session(
        uid: str,
        topic_slug: str,
        text: str,
        ephemeral: bool,
        client: HermesClient,
    ) -> str:
        """Create the session for a first turn; return its id.

        Ephemeral (#246): an incognito chat is created with the `[temp:]` marker
        (kept out of the durable list, deleted on close) plus a unique title
        suffix after the marker (#286 — so two temp chats can't collide on
        Hermes' unique-title constraint), is NOT bound to a topic, NOT re-titled
        (re-titling would re-stamp the `[uid:]` marker and surface it), and never
        has a `session_topics` row — it carries no durable state. Normal chats
        bind a primary topic and persist the auto-title.

        No system_prompt overlay or model override is passed (#293): the
        gateway's profile supplies the soul + the base model, so an empty create
        lets the profile decide instead of fighting it.

        `client` is the gateway the caller routed to (#293): household for a
        resident chat (the common case), or the admin gateway when an admin
        selected the admin persona. An admin-gateway create is recorded in
        `admin_sessions` so the session's follow-up turns route back to it.
        """
        if ephemeral:
            # A unique suffix rides after the `[temp:]` marker so a second temp
            # chat can't 400 against the first's bare-marker title (#286, same
            # collision #267/#277 fixed). The marker prefix is preserved, so the
            # chat stays incognito (not-persisted / not-listed).
            session_id = await client.create_session(
                uid,
                ephemeral=True,
                title=_title_from(text),
            )
            log.info(
                "chat.session.created", uid=uid, session_id=session_id, ephemeral=True
            )
            return session_id
        primary = new_session_topic(topic_slug)
        # Born with a unique marker-embedded title (not the bare `[uid:...]`
        # marker), so a first turn can never 400 against an abandoned
        # bare-marker stub already holding it — the same collision #267 fixed
        # for the compaction path, here on the main first-turn path (#277).
        session_id = await client.create_session(uid, title=_title_from(text))
        if client is admin_gw and client is not household_gw:
            admin_sessions.add(session_id)
        log.info(
            "chat.session.created",
            uid=uid,
            session_id=session_id,
            topic=primary or "",
        )
        if primary:
            topics_store.set_primary(solilos_db_path, session_id, primary, uid)
        return session_id

    def topic_turn_text(
        text: str, uid: str, session_id: str, *, ephemeral: bool, extract_topic: str
    ) -> str:
        """Prepend the active-topic / ephemeral context hint to a turn.

        Normal chat: data ingested from a topic-T chat must be stamped
        `#topic/<slug>` so it is retrievable by topic (#243). The proxy surfaces
        the chat's primary topic as a leading system-context line; any ingestion
        skill in the turn reads it and tags its note. Non-topic chats are
        untouched (no hint).

        Ephemeral chat (#246): the session is incognito, so the proxy does NOT
        consult `session_topics` (an ephemeral chat keeps no durable assignment)
        and instead injects the ephemeral guard hint that tells the agent to
        persist nothing. The one durable escape hatch is an explicit extract:
        when the turn carries `extract_topic`, the topic stamp is appended so the
        single note the agent writes is tagged `#topic/<slug>` — that note is the
        only durable output of the whole conversation.
        """
        if ephemeral:
            parts = [_now_hint(), _EPHEMERAL_HINT]
            if extract_topic:
                display = topics_store.display_name(solilos_db_path, extract_topic)
                label = display or extract_topic
                parts.append(
                    f"[Extract this to a note #topic/{extract_topic} ({label})]"
                )
            parts.append(text)
            return "\n\n".join(parts)
        hint = topics_store.topic_context_hint(solilos_db_path, session_id, uid)
        parts = [_now_hint()]
        if hint:
            parts.append(hint)
        parts.append(text)
        return "\n\n".join(parts)

    def persist_mentions(
        uid: str, session_id: str, text: str, *, ephemeral: bool
    ) -> None:
        """Parse + store the turn's `#tag`/`@person` mentions (#279).

        Skipped for ephemeral chats (they keep no durable state, like topics).
        Degrades to no-op when the DB/table is absent (mentions_store handles it).
        """
        if ephemeral:
            return
        tags, persons = parse_mentions(text)
        mentions_store.record_mentions(solilos_db_path, session_id, uid, tags, persons)

    async def persist_turn_trace(
        uid: str, session_id: str, t0: float, *, ephemeral: bool
    ) -> None:
        """Correlate this turn's Ollama calls to a stable trace_id and persist.

        The trace proxy sees the calls but not the Hermes session id; only this
        turn knows both the session and its wall-clock window — `t0` (captured at
        turn start) to now (the proxy has recorded every call by the time the
        reply is back, so the fetch moment is a safe upper bound, #306). Pull the
        proxy's calls in that window, assign them a fresh per-turn `trace_id`, and
        persist the ordered steps so a reopen shows the same trace. Skipped for
        ephemeral chats (no durable state); fail-open — a proxy/DB hiccup never
        breaks the turn that already produced a reply.
        """
        if ephemeral:
            return
        try:
            steps = await fetch_turn_steps(t0)
            if steps:
                trace_store.persist_trace(
                    solilos_db_path, session_id, uuid.uuid4().hex, uid, steps
                )
        except Exception as e:  # noqa: BLE001 — trace persistence is best-effort
            log.warn("chat.trace.persist_error", session_id=session_id, error=str(e))

    async def fetch_turn_steps(t0: float) -> list[dict[str, Any]]:
        """The proxy's captured Ollama calls for this turn — those whose timestamp
        is at or after the turn start `t0` and at or before the fetch moment `t1`
        (a record can't post-date the GET that read it). `/__traces__` returns
        newest-first light records (each with `ts`, `model`, `wall_s`, tokens,
        `context_free`, `finish_reason`, `n_tools`, and the `id` that fetches the
        exact content) — we keep the window's calls in chronological (step) order
        and tag the proxy `id` as `detail_id` for the per-step content fetch."""
        url = f"{trace_proxy_url.rstrip('/')}/__traces__"
        timeout = aiohttp.ClientTimeout(total=5)
        async with aiohttp.ClientSession(timeout=timeout) as client:
            async with client.get(url) as r:
                if r.status >= 400:
                    return []
                records = await r.json()
        t1 = time.time()
        window = [rec for rec in records if t0 <= rec.get("ts", 0) <= t1]
        window.sort(key=lambda rec: rec.get("ts", 0))
        return [
            {
                "model": rec.get("model"),
                "profile": rec.get("profile"),
                "wall_s": rec.get("wall_s"),
                "prompt_tokens": rec.get("prompt_tokens"),
                "completion_tokens": rec.get("completion_tokens"),
                "context_free": rec.get("context_free"),
                "finish_reason": rec.get("finish_reason"),
                "n_tools": rec.get("n_tools"),
                "detail_id": rec.get("id"),
            }
            for rec in window
        ]

    async def session_trace(request: web.Request) -> web.Response:
        # The persisted per-turn LLM trace for one chat (#306): the ordered steps
        # the proxy captured, each with model/wall_s/tokens/detail_id, so the
        # panel renders the same trace on reopen. Per-resident scope.
        uid = resolve_uid(request, remote_user_header, default_uid)
        session_id = request.match_info["session_id"]
        steps = trace_store.list_session_trace(solilos_db_path, session_id, uid)
        return web.json_response({"ok": True, "steps": steps})

    async def trace_detail(request: web.Request) -> web.Response:
        # Exact per-call content for one trace step (#307 panel → #305 detail):
        # the browser can't reach the proxy port directly, so the chat server
        # passes its `/__traces__/<id>` through. The detail store is the proxy's
        # FIFO ring, so an old turn may 404 — the panel degrades to no modal.
        detail_id = request.match_info["detail_id"]
        url = f"{trace_proxy_url.rstrip('/')}/__traces__/{detail_id}"
        timeout = aiohttp.ClientTimeout(total=5)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as client:
                async with client.get(url) as r:
                    body = await r.read()
                    return web.Response(
                        body=body, status=r.status, content_type="application/json"
                    )
        except aiohttp.ClientError:
            return web.json_response({"error": "trace proxy unavailable"}, status=502)

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
                "context_window": context_window.value,
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

    async def list_mcp(_request: web.Request) -> web.Response:
        # MCP servers aren't in Hermes' /v1/toolsets; the sidecar reports them
        # (name/url/reachable/tools, no tokens) from config.yaml.
        servers = await _agent_get_mcp(config_agent_url, agent_token)
        if servers is None:
            return web.json_response(
                {"ok": False, "reason": "agent_unavailable"}, status=502
            )
        return web.json_response({"ok": True, "servers": servers})

    async def test_mcp(request: web.Request) -> web.Response:
        # Interactive Tools-panel tester (#191): run one MCP tool with operator
        # args. Admin-gated — invoking a tool can mutate (e.g. restart_service),
        # so it carries the same gate as the other write controls.
        if not is_admin(request, remote_groups_header, admin_group):
            return web.json_response({"ok": False, "reason": "forbidden"}, status=403)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 — any malformed JSON
            return web.json_response(
                {"ok": False, "reason": "invalid_json"}, status=400
            )
        tool = body.get("tool")
        if not isinstance(tool, str) or not tool.strip():
            return web.json_response({"ok": False, "reason": "empty_tool"}, status=400)
        arguments = body.get("arguments")
        if arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            return web.json_response(
                {"ok": False, "reason": "invalid_arguments"}, status=400
            )
        result = await _agent_test_mcp(
            config_agent_url,
            agent_token,
            request.match_info["server"],
            tool.strip(),
            arguments,
        )
        if result is None:
            return web.json_response(
                {"ok": False, "reason": "agent_unavailable"}, status=502
            )
        log.info(
            "chat.mcp.test",
            uid=resolve_uid(request, remote_user_header, default_uid),
            server=request.match_info["server"],
            tool=tool.strip(),
            ok=bool(result.get("ok")),
        )
        return web.json_response(result)

    async def cancel_chat(request: web.Request) -> web.Response:
        # Interrupt an in-flight stream for a session (#192). Sets the cancel
        # event the stream loop polls; the loop then stops reading from Hermes
        # and closes that connection, releasing the model run.
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 — any malformed JSON
            body = {}
        session_id = str((body or {}).get("session_id") or "")
        event = cancels.get(session_id) if session_id else None
        if event is None:
            return web.json_response({"ok": True, "cancelled": False})
        event.set()
        log.info(
            "chat.stream.cancelled",
            uid=resolve_uid(request, remote_user_header, default_uid),
            session_id=session_id,
        )
        return web.json_response({"ok": True, "cancelled": True})

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
        # Annotate each session with its primary topic so the list can render a
        # chip (#241). Per-resident scope (D3): only the caller's assignments.
        ids = [str(s.get("id")) for s in sessions if s.get("id")]
        primaries = topics_store.primary_topics_for(solilos_db_path, ids, uid)
        for s in sessions:
            s["primary_topic"] = primaries.get(str(s.get("id")))
        return web.json_response({"ok": True, "sessions": sessions})

    async def create_session(request: web.Request) -> web.Response:
        uid = resolve_uid(request, remote_user_header, default_uid)

        # The ServiceBay-maintenance lock (#229) is keyed off the URL QUERY
        # STRING, not the POST body: the iframe `src` is set by ServiceBay and
        # in-frame JS cannot rewrite it, so a request body can never forge the
        # maintenance persona — and, conversely, can never escape the lock once
        # ServiceBay has set the query.
        if request.rel_url.query.get("persona") == personalities.MAINTENANCE_ID:
            if not is_admin(request, remote_groups_header, admin_group):
                return web.json_response(
                    {"ok": False, "reason": "forbidden"}, status=403
                )
            # Option B: the locked system prompt is the LIVE admin SOUL.md
            # (#175/#176), fetched through the sidecar. Any `personality` in the
            # body is ignored — the lock cannot be overridden by the client.
            soul = await _agent_get_soul(config_agent_url, agent_token)
            if not soul or not soul.strip():
                # Fail safe: never silently fall back to the household Sol
                # persona (that would leak the resident-facing soul into a
                # maintenance session). Refuse the session instead.
                log.error("chat.maint.soul_unavailable", uid=uid)
                return web.json_response(
                    {"ok": False, "reason": "soul_unavailable"}, status=502
                )
            try:
                session_id = await admin_gw.create_session(uid, soul, maintenance=True)
            except HermesError:
                return web.json_response(
                    {"ok": False, "reason": "hermes_unavailable"}, status=502
                )
            # Pin this session to the admin gateway so its follow-up turns route
            # back to the same instance (Hermes session state is per-gateway).
            admin_sessions.add(session_id)
            log.info(
                "chat.session.created",
                uid=uid,
                session_id=session_id,
                personality=personalities.MAINTENANCE_ID,
            )
            return web.json_response({"ok": True, "session_id": session_id})

        # No system_prompt overlay (#293): the household gateway's profile owns
        # the soul, so an empty create lets the profile supply it instead of a
        # per-session persona overlay that would fight it.
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
        messages = session.get("messages") or []
        attach_to_messages(messages, attachments.batches(session_id))
        # Hide the internal-hint prefixes the proxy injected into each user turn
        # so history shows what the resident actually typed (#309).
        for m in messages:
            if m.get("role") == "user":
                m["content"] = strip_internal_hints(m.get("content") or "")
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
        attachments.delete(session_id)
        log.info(
            "chat.session.deleted",
            uid=resolve_uid(request, remote_user_header, default_uid),
            session_id=session_id,
        )
        return web.json_response({"ok": True})

    async def list_topics(request: web.Request) -> web.Response:
        uid = resolve_uid(request, remote_user_header, default_uid)
        return web.json_response(
            {"ok": True, "topics": topics_store.list_topics(solilos_db_path, uid)}
        )

    async def create_topic(request: web.Request) -> web.Response:
        # Create a resident-scoped topic from a confirmed suggestion (D4, #245).
        # The topic-suggester skill POSTs here only after the resident says yes;
        # the proxy never auto-creates. Idempotent on slug (see topics_store).
        uid = resolve_uid(request, remote_user_header, default_uid)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 — any malformed JSON
            return web.json_response(
                {"ok": False, "reason": "invalid_json"}, status=400
            )
        slug = str(body.get("slug") or "").strip().strip("/")
        display_name = str(body.get("display_name") or "").strip()
        if not slug or not display_name:
            return web.json_response(
                {"ok": False, "reason": "slug_and_display_name_required"}, status=400
            )
        color = body.get("color")
        color = color.strip() if isinstance(color, str) and color.strip() else None
        topics_store.create_topic(solilos_db_path, slug, display_name, uid, color)
        log.info("chat.topic.create", uid=uid, slug=slug)
        return web.json_response({"ok": True, "slug": slug})

    async def get_session_topics(request: web.Request) -> web.Response:
        uid = resolve_uid(request, remote_user_header, default_uid)
        session_id = request.match_info["session_id"]
        assigned = topics_store.get_session_topics(solilos_db_path, session_id, uid)
        return web.json_response({"ok": True, **assigned})

    async def set_session_topics(request: web.Request) -> web.Response:
        uid = resolve_uid(request, remote_user_header, default_uid)
        session_id = request.match_info["session_id"]
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 — any malformed JSON
            return web.json_response(
                {"ok": False, "reason": "invalid_json"}, status=400
            )
        action = body.get("action")
        slug = body.get("slug")
        if not isinstance(slug, str) or not slug.strip():
            return web.json_response({"ok": False, "reason": "empty_slug"}, status=400)
        slug = slug.strip()
        if action == "primary":
            topics_store.set_primary(solilos_db_path, session_id, slug, uid)
        elif action == "add_secondary":
            topics_store.add_secondary(solilos_db_path, session_id, slug, uid)
        elif action == "remove":
            topics_store.remove_topic(solilos_db_path, session_id, slug, uid)
        else:
            return web.json_response(
                {"ok": False, "reason": "invalid_action"}, status=400
            )
        log.info(
            "chat.session.topic",
            uid=uid,
            session_id=session_id,
            action=action,
            slug=slug,
        )
        assigned = topics_store.get_session_topics(solilos_db_path, session_id, uid)
        return web.json_response({"ok": True, **assigned})

    async def topic_items(request: web.Request) -> web.Response:
        # The topic dashboard's per-topic note list (#244): the notes tagged
        # `#topic/<slug>` in the vault (stamped by ingestion, #243). Per-resident
        # scope (D3): only the caller's own (or unowned/shared) notes. The slug
        # may be hierarchical (projekt/wintergarten), so the route captures the
        # rest of the path into `slug`.
        uid = resolve_uid(request, remote_user_header, default_uid)
        slug = request.match_info["slug"].strip("/")
        items = notes_search.notes_for_topic(notes_dir, slug, uid)
        return web.json_response({"ok": True, "slug": slug, "items": items})

    async def mentions_tags(request: web.Request) -> web.Response:
        # Autosuggest for `#tag` (#279): the resident's already-used tags,
        # prefix-filtered. Per-resident scope (owner_uid = resolve_uid).
        uid = resolve_uid(request, remote_user_header, default_uid)
        prefix = request.rel_url.query.get("q", "").strip().lstrip("#").lower()
        values = mentions_store.known_tags_for(solilos_db_path, uid, prefix)
        return web.json_response(
            {"ok": True, "tags": [{"kind": "tag", "value": v} for v in values]}
        )

    async def mentions_persons(request: web.Request) -> web.Response:
        # Autosuggest for `@person` (#279): used persons unioned with the seed
        # (residents/uid registry + manual list; CardDAV later, #207). The
        # caller's own uid is always a resident; other residents come from config.
        uid = resolve_uid(request, remote_user_header, default_uid)
        prefix = request.rel_url.query.get("q", "").strip().lstrip("@").lower()
        seed = seeded_persons([uid, *resident_uids])
        used = mentions_store.known_persons_for(solilos_db_path, uid)
        merged = _dedup([*used, *seed])
        if prefix:
            merged = [v for v in merged if v.startswith(prefix)]
        merged.sort()
        return web.json_response(
            {"ok": True, "persons": [{"kind": "person", "value": v} for v in merged]}
        )

    async def session_mentions(request: web.Request) -> web.Response:
        # The tag-cloud for one chat (#279c): the resident's `#tag` / `@person`
        # mentions in this session, each with the message_ref that carried it
        # (first appearance) for jump-to-message. Per-resident scope.
        uid = resolve_uid(request, remote_user_header, default_uid)
        session_id = request.match_info["session_id"]
        items = mentions_store.list_session_mentions(solilos_db_path, session_id, uid)
        return web.json_response({"ok": True, "mentions": items})

    async def chat(request: web.Request) -> web.Response:
        uid = resolve_uid(request, remote_user_header, default_uid)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 — any malformed JSON
            return web.json_response(
                {"ok": False, "reason": "invalid_json"}, status=400
            )

        images = _images_from(body)
        text = str(body.get("input") or "").strip()
        if not text and not images:
            return web.json_response({"ok": False, "reason": "empty_input"}, status=400)
        if not text:
            text = _IMAGE_PROMPT
        session_id = str(body.get("session_id") or "")
        topic_slug = str(body.get("topic") or "").strip()
        ephemeral = bool(body.get("ephemeral"))
        effort = reasoning.choose_effort(
            text,
            selector=body.get("reasoning"),
            admin=is_admin(request, remote_groups_header, admin_group),
        )
        client = gateway_for(request, session_id, body.get("personality"))

        clock = asyncio.get_event_loop().time
        t_start = clock() * 1000.0
        wall_t0 = time.time()  # wall-clock window for proxy trace correlation (#306)
        compacted = False
        try:
            # Only a missing session_id starts a fresh Hermes session; turn 2+
            # carry the same id back, so consecutive turns reuse one warm
            # Hermes session (and its KV prefix cache). A cold turn-2 TTFT is
            # therefore Ollama model eviction, not a per-turn session (#268).
            if not session_id:
                session_id = await create_turn_session(
                    uid,
                    topic_slug,
                    text,
                    ephemeral,
                    client,
                )
            elif not ephemeral:
                session_id, compacted = await maybe_compact(uid, session_id, client)
            turn_text = topic_turn_text(
                text, uid, session_id, ephemeral=ephemeral, extract_topic=topic_slug
            )
            persist_mentions(uid, session_id, text, ephemeral=ephemeral)
            reply = await client.chat(session_id, turn_text, images, effort)
        except HermesError:
            return web.json_response(
                {"ok": False, "reason": "hermes_unavailable"}, status=502
            )
        attachments.add(session_id, images)
        await persist_turn_trace(uid, session_id, wall_t0, ephemeral=ephemeral)
        # Non-streamed turn: only total wall-time is observable (no per-phase
        # boundaries without the stream), so the trace carries just the total
        # (#225). The streaming path is where the phase waterfall comes from.
        total_ms = clock() * 1000.0 - t_start
        trace = _trace_from_phases([], total_ms)

        return web.json_response(
            {
                "ok": True,
                "session_id": session_id,
                "reply": reply,
                "trace": trace,
                "compacted": compacted,
            }
        )

    async def chat_stream(request: web.Request) -> web.StreamResponse:
        uid = resolve_uid(request, remote_user_header, default_uid)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 — any malformed JSON
            return web.json_response(
                {"ok": False, "reason": "invalid_json"}, status=400
            )

        images = _images_from(body)
        text = str(body.get("input") or "").strip()
        if not text and not images:
            return web.json_response({"ok": False, "reason": "empty_input"}, status=400)
        if not text:
            text = _IMAGE_PROMPT
        session_id = str(body.get("session_id") or "")
        topic_slug = str(body.get("topic") or "").strip()
        ephemeral = bool(body.get("ephemeral"))
        effort = reasoning.choose_effort(
            text,
            selector=body.get("reasoning"),
            admin=is_admin(request, remote_groups_header, admin_group),
        )
        client = gateway_for(request, session_id, body.get("personality"))

        resp = web.StreamResponse(
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            }
        )
        await resp.prepare(request)

        cancel = asyncio.Event()
        # Phase timing for the latency trace (#225). Timestamps are monotonic
        # ms; only the boundaries the proxy can actually see on the wire are
        # captured (see _trace_from_phases for what is/isn't observable).
        clock = asyncio.get_event_loop().time
        t_start = clock() * 1000.0
        wall_t0 = time.time()  # wall-clock window for proxy trace correlation (#306)
        t_first: float | None = None  # first delta -> prefill / TTFT
        t_think_end: float | None = None  # </thinking> seen -> reasoning ends
        tool_ms = 0.0
        t_tool: float | None = None  # open tool round-trip
        answer_buf = ""
        cancelled = False
        try:
            compacted = False
            if not session_id:
                session_id = await create_turn_session(
                    uid,
                    topic_slug,
                    text,
                    ephemeral,
                    client,
                )
            elif not ephemeral:
                session_id, compacted = await maybe_compact(uid, session_id, client)
            cancels[session_id] = cancel
            await _send_event(
                resp, "session", {"session_id": session_id, "compacted": compacted}
            )
            # Persist the attachment once the turn is under way (Hermes has the
            # user message; we hold the pixels it drops) so history re-renders
            # the thumbnail after a refresh (#202).
            attachments.add(session_id, images)
            turn_text = topic_turn_text(
                text, uid, session_id, ephemeral=ephemeral, extract_topic=topic_slug
            )
            persist_mentions(uid, session_id, text, ephemeral=ephemeral)
            stream = client.chat_stream(session_id, turn_text, images, effort)
            async for event in _heartbeat(stream, resp):
                if cancel.is_set():
                    # Closing the upstream generator aborts the Hermes/Ollama
                    # run (#192) — stops generation, not just our forwarding.
                    await stream.aclose()
                    await _send_event(resp, "cancelled", {})
                    cancelled = True
                    break
                name, data = _normalize(event)
                now = clock() * 1000.0
                if name == "delta":
                    if t_first is None:
                        t_first = now
                    answer_buf += data.get("text", "")
                    if t_think_end is None and _THINK_CLOSE in answer_buf.lower():
                        t_think_end = now
                elif name == "tool":
                    if data.get("phase") == "started":
                        t_tool = now
                    elif t_tool is not None:
                        tool_ms += now - t_tool
                        t_tool = None
                elif name == "completed":
                    # gemma4 returns its reasoning on every run.completed (the
                    # `reasoning_content` field), regardless of effort — so the
                    # block is gated here on the per-turn effort, not on Hermes:
                    # a fast ("none") turn surfaces nothing (#222); a thorough
                    # turn emits a distinct `reasoning` event the panel renders
                    # collapsibly (#231). The forwarded `completed` stays bare.
                    reasoning_text = data.pop("reasoning", "")
                    if effort != "none" and reasoning_text:
                        await _send_event(resp, "reasoning", {"text": reasoning_text})
                    # A tool-invocation turn (e.g. a Home Assistant state query)
                    # streams no answer deltas — the final summary arrives only
                    # on run.completed. Surface it as a late delta so the browser
                    # renders the reply instead of an empty bubble (#258).
                    completed_answer = data.pop("answer", "")
                    if not answer_buf.strip() and completed_answer:
                        await _send_event(resp, "delta", {"text": completed_answer})
                        answer_buf += completed_answer
                await _send_event(resp, name, data)
            if not cancelled:
                t_end = clock() * 1000.0
                trace = _trace_from_phases(
                    _stream_phases(t_start, t_first, t_think_end, t_end, tool_ms),
                    t_end - t_start,
                )
                await _send_event(resp, "trace", trace)
                await persist_turn_trace(uid, session_id, wall_t0, ephemeral=ephemeral)
        except HermesError:
            await _send_event(resp, "error", {"reason": "hermes_unavailable"})
        finally:
            cancels.pop(session_id, None)
        await _send_event(resp, "done", {})
        return resp

    @web.middleware
    async def csp(request: web.Request, handler: Any) -> web.StreamResponse:
        # CSP frame-ancestors gates who may iframe the chat (#228). Set on
        # every response; no X-Frame-Options (it conflicts with CSP).
        resp = await handler(request)
        resp.headers["Content-Security-Policy"] = f"frame-ancestors {frame_ancestors}"
        return resp

    app = web.Application(middlewares=[csp])
    app.router.add_get("/", index)
    app.router.add_get("/health", health)
    app.router.add_get("/api/whoami", whoami)
    app.router.add_get("/api/toolsets", list_toolsets)
    app.router.add_get("/api/mcp", list_mcp)
    app.router.add_post("/api/mcp/{server}/test", test_mcp)
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
    app.router.add_get("/api/topics", list_topics)
    app.router.add_post("/api/topics", create_topic)
    app.router.add_get("/api/sessions/{session_id}/topics", get_session_topics)
    app.router.add_post("/api/sessions/{session_id}/topics", set_session_topics)
    app.router.add_get("/api/topics/{slug:.+}/items", topic_items)
    app.router.add_get("/api/mentions/tags", mentions_tags)
    app.router.add_get("/api/mentions/persons", mentions_persons)
    app.router.add_get("/api/sessions/{session_id}/mentions", session_mentions)
    app.router.add_get("/api/sessions/{session_id}/trace", session_trace)
    app.router.add_get("/__traces__/{detail_id}", trace_detail)
    app.router.add_post("/api/chat", chat)
    app.router.add_post("/api/chat/stream", chat_stream)
    app.router.add_post("/api/chat/cancel", cancel_chat)
    app.router.add_static("/static/", STATIC_DIR)
    return app


# Legacy boundary marker for the latency trace (#225): if a model ever streams
# an inline `</thinking>` close tag in the answer deltas this splits reasoning
# from answer. gemma4 does NOT — it surfaces reasoning as a distinct field on
# run.completed (#231), delivered in one shot at turn end — so this no longer
# fires for gemma4 and the reasoning phase simply folds into the answer span.
_THINK_CLOSE = "</think"


def _stream_phases(
    t_start: float,
    t_first: float | None,
    t_think_end: float | None,
    t_end: float,
    tool_ms: float,
) -> list[tuple[str, float]]:
    """Turn the stream timestamps into labelled phase spans (#225).

    What the proxy can genuinely time, in order: prefill (turn start → first
    token), reasoning (first token → `</thinking>`, only when a block streamed),
    answer (reasoning end / first token → turn end), and the summed tool
    round-trips. The Ollama-internal prefill/eval token split is NOT here — it
    is invisible to the proxy (see _trace_from_phases).
    """
    if t_first is None:  # no tokens streamed (e.g. tool-only or empty turn)
        return [("Tool round-trip", tool_ms)] if tool_ms > 0 else []
    phases: list[tuple[str, float]] = [("Prefill (TTFT)", t_first - t_start)]
    answer_start = t_first
    if t_think_end is not None:
        phases.append(("Reasoning", t_think_end - t_first))
        answer_start = t_think_end
    phases.append(("Answer", t_end - answer_start))
    if tool_ms > 0:
        phases.append(("Tool round-trip", tool_ms))
    return phases


def _trace_from_phases(
    phases: list[tuple[str, float]], total_ms: float
) -> dict[str, Any]:
    """Assemble a per-turn latency trace from measured phase durations (#225).

    `phases` is `[(label, ms), ...]` for the spans the proxy could actually
    time on the wire — what it observes is the Hermes *session stream*, so the
    honest, measurable breakdown is: time-to-first-token (prefill), reasoning
    generation (the `<thinking>` block, when one streamed), answer generation,
    and tool round-trips (`tool.started`→`tool.completed`). The fine-grained
    Ollama prompt_eval/eval (prefill vs decode token) split happens *inside*
    Hermes and is never streamed to this proxy, so it is deliberately absent —
    it would need Hermes to expose per-pass timings to be shown.

    Each phase becomes `{label, seconds, pct}` (pct of total wall-time, so a
    sum < 100% is expected — the gaps are orchestration the proxy can't
    attribute). Zero/negative spans are dropped so the waterfall stays honest.
    """
    total = max(total_ms, 0.0)
    out = []
    for label, ms in phases:
        if ms <= 0:
            continue
        pct = (ms / total * 100.0) if total else 0.0
        out.append(
            {"label": label, "seconds": round(ms / 1000.0, 2), "pct": round(pct, 1)}
        )
    return {"total_seconds": round(total / 1000.0, 2), "phases": out}


def _images_from(body: Any) -> list[str]:
    """Pull image-attachment data URLs from a chat body (#183).

    The browser sends `data:image/...;base64,<b64>` URLs. Hermes' session-chat
    consumes images as OpenAI `image_url` parts and requires the *full* data URL
    (the `data:` prefix must stay — stripping it makes Hermes reject the part as
    a non-image payload, #202), so we keep each URL as-is. Non-strings, empties,
    and anything past `_MAX_IMAGES` are dropped.
    """
    raw = body.get("images") if isinstance(body, dict) else None
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            continue
        out.append(item)
        if len(out) >= _MAX_IMAGES:
            break
    return out


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


async def _agent_get_mcp(agent_url: str, token: str) -> list[dict[str, Any]] | None:
    """Read the MCP servers (name/url/reachable/tools, no tokens) from the
    sidecar. None on failure."""
    url = f"{agent_url.rstrip('/')}/mcp"
    try:
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as client:
            async with client.get(url, headers=_agent_headers(token)) as r:
                if r.status >= 400:
                    return None
                body = await r.json()
        servers = body.get("servers") if isinstance(body, dict) else None
        return servers if isinstance(servers, list) else []
    except (aiohttp.ClientError, TimeoutError, OSError, ValueError) as e:
        log.error("chat.agent.unreachable", op="get_mcp", error=str(e))
        return None


async def _agent_test_mcp(
    agent_url: str, token: str, server: str, tool: str, arguments: dict[str, Any]
) -> dict[str, Any] | None:
    """Ask the sidecar to invoke `tool` on MCP `server` (#191). The agent's
    JSON ({ok, result|error}) on 2xx, None when the sidecar is unreachable."""
    url = f"{agent_url.rstrip('/')}/mcp/{server}/test"
    try:
        timeout = aiohttp.ClientTimeout(total=35)
        async with aiohttp.ClientSession(timeout=timeout) as client:
            async with client.post(
                url,
                json={"tool": tool, "arguments": arguments},
                headers=_agent_headers(token),
            ) as r:
                if r.status == 404:
                    return {"ok": False, "error": "Unknown MCP server"}
                if r.status >= 400:
                    detail = (await r.text())[:300]
                    log.error(
                        "chat.agent.error", op="test_mcp", status=r.status, body=detail
                    )
                    return {"ok": False, "error": f"Agent returned HTTP {r.status}"}
                return await r.json()
    except (aiohttp.ClientError, TimeoutError, OSError, ValueError) as e:
        log.error("chat.agent.unreachable", op="test_mcp", error=str(e))
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


# A tool-invocation turn runs two sequential Ollama passes (tool-selection, then
# the answer) with a tool round-trip between them — Hermes streams nothing for
# the whole prefill of each pass, which on a busy GPU is well over a minute of
# dead air. The browser's streaming fetch (and any reverse proxy in front) drops
# an idle connection long before the late answer arrives (#319), so we emit a
# keepalive frame whenever the upstream is silent for this long. The client
# ignores the unknown event; it only keeps the connection warm.
_HEARTBEAT_S = 10.0


async def _heartbeat(
    stream: AsyncIterator[dict[str, Any]], resp: web.StreamResponse
) -> AsyncIterator[dict[str, Any]]:
    """Forward `stream`'s events, emitting a keepalive on every silent gap."""
    it = stream.__aiter__()
    nxt: asyncio.Future[dict[str, Any]] | None = None
    try:
        while True:
            nxt = asyncio.ensure_future(it.__anext__())
            while True:
                try:
                    event = await asyncio.wait_for(asyncio.shield(nxt), _HEARTBEAT_S)
                except TimeoutError:
                    await _send_event(resp, "keepalive", {})
                    continue
                except StopAsyncIteration:
                    nxt = None
                    return
                break
            nxt = None
            yield event
    finally:
        if nxt is not None:
            nxt.cancel()


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
        return "completed", {
            "reasoning": _reasoning_from_completed(payload),
            "answer": _answer_from_messages(payload.get("messages")),
        }
    return "keepalive", {}


def _reasoning_from_completed(payload: dict[str, Any]) -> str:
    """Pull the reasoning text out of a `run.completed` payload (#231).

    gemma4 does NOT emit a literal `<thinking>` tag inline in the answer
    deltas; it surfaces the reasoning as a distinct field on the final
    assistant message of the `run.completed` event — `reasoning_content`
    (preferred) or `reasoning`. Both carry the same text; the answer text is in
    `content`, separate from the reasoning. Empty string when absent.
    """
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return ""
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        text = msg.get("reasoning_content") or msg.get("reasoning")
        if text:
            return str(text)
    return ""


async def serve(
    host: str,
    port: int,
    *,
    hermes: HermesClient,
    hermes_admin: HermesClient | None = None,
    remote_user_header: str,
    default_uid: str,
    remote_groups_header: str = "Remote-Groups",
    admin_group: str = "admins",
    skills_dir: str = "/data/skills",
    soul_path: str = "/data/SOUL.md",
    config_agent_url: str = "http://127.0.0.1:8650",
    agent_token: str = "",
    logout_url: str = "",
    context_window: ContextWindow,
    compaction_threshold: float = compaction.DEFAULT_THRESHOLD,
    attachments_dir: str = "/data/attachments",
    frame_ancestors: str = "'self'",
    fast_model: str = "",
    thorough_model: str = "",
    solilos_db_path: str = "/var/lib/solilos/solilos.db",
    notes_dir: str = "/opt/data/notes",
    trace_proxy_url: str = "http://127.0.0.1:11436",
) -> None:
    if isinstance(context_window, int):
        context_window = ContextWindow.static(context_window)
    app = build_app(
        hermes=hermes,
        hermes_admin=hermes_admin,
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
        compaction_threshold=compaction_threshold,
        attachments_dir=attachments_dir,
        frame_ancestors=frame_ancestors,
        fast_model=fast_model,
        thorough_model=thorough_model,
        solilos_db_path=solilos_db_path,
        notes_dir=notes_dir,
        trace_proxy_url=trace_proxy_url,
    )
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    log.info("chat.listening", host=host, port=port)
    # Re-derive the context window periodically so a model switch adapts the
    # compaction cap without a restart (no-op when an explicit override pins it).
    refresh = asyncio.create_task(context_window.refresh_loop())
    try:
        await asyncio.Event().wait()
    finally:
        refresh.cancel()
        await runner.cleanup()

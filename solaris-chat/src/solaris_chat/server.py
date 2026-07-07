"""aiohttp app: the chat surface over the in-process Solaris Engine.

The browser keeps the current session id and sends it back with each turn;
on the first turn (no id) the server creates a session bound to the SSO
identity and returns the id. Chat/session state lives in solaris.db via the
engine's store; the server itself stays a thin routing layer.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sqlite3
import time
import uuid
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from aiohttp import web

from solaris_chat import (
    compaction,
    favorites_store,
    mentions_store,
    notes_search,
    personalities,
    reasoning,
    settings_store,
    skills,
    topics_store,
    trace_store,
)
from solaris_chat.attachments import AttachmentStore, attach_to_messages
from solaris_chat.context import STATIC_DEFAULT, ContextWindow
from solaris_chat.engine import confirm, store
from solaris_chat.engine.client import EngineClient, EngineError, current_uid
from solaris_chat.engine import vram
from solaris_chat.engine.facade import add_facade_routes
from solaris_chat.engine.ollama import OllamaChat, OllamaError
from solaris_chat.engine.knowledge import okf, projection
from solaris_chat.engine.tools.favorites import PINNABLE_TOOLS
from solaris_chat.engine.areas import AreaRegistry
from solaris_chat.engine.tools.ha import (
    _SERVICE_ALIASES,
    call_service_scoped,
    fetch_addable_cards,
    fetch_card,
    fetch_energy,
    fetch_energy_history,
    fetch_entity_names,
)
from solaris_chat.engine.tools.mcp_tools import McpToolbox
from solaris_chat.logging import log

STATIC_DIR = Path(__file__).parent / "static"


def _read_okf(notes_dir: str, okf_path: str) -> dict[str, str]:
    """Read an OKF concept file's description + body for the concept page (#502).

    `okf_path` is the projection-stored vault-relative path; resolve it under
    `notes_dir`, refusing any path that escapes the vault. Empty on any error so
    the page degrades to the projected facts/events.
    """
    root = Path(notes_dir).resolve()
    try:
        target = (root / okf_path).resolve()
        target.relative_to(root)
        return okf.read_concept(target.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return {"description": "", "body": ""}


def _favorite_label(payload: dict[str, Any]) -> str:
    """A short label for a usage-counted action (#646) — the tool's most telling
    argument, else the tool name. `payload` is `{tool, args}` from the counter."""
    args = payload.get("args") if isinstance(payload.get("args"), dict) else {}
    tool = str(payload.get("tool") or "")
    if tool == "ha_call_service":
        entity_id = str(args.get("entity_id") or "")
        name = (
            entity_id.split(".", 1)[1].replace("_", " ")
            if "." in entity_id
            else entity_id
        )
        service = str(args.get("service") or "")
        return f"{name} — {service}".strip(" —") or tool
    for key in ("query", "station", "title", "name"):
        val = args.get(key)
        if val:
            return str(val)
    return tool or "Aktion"


# Default prompt for an image-only turn (attachment with no typed text), so the
# media-ingestion hook has a turn to trigger on. Mirrors the German tone the
# hook itself uses with residents.
_IMAGE_PROMPT = "Bitte sieh dir dieses Bild an und verarbeite es."
# The lifecycle event an image-only turn fires; the hook that acts on it is
# resolved from the registry (not a hardcoded id) so rebinding it in the
# `/hooks` editor changes which definition handles the upload.
_IMAGE_UPLOAD_EVENT = "image-upload"
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

# The built-in household topic slug (mirrors the frontend `HOUSEHOLD_TOPIC`). A
# chat whose primary topic is this is the pinned "Zuhause" household chat: it is
# pinned to the fast e2b household gateway and never offers thinking, regardless
# of the everyday-chat model preference.
HOUSEHOLD_TOPIC = "household"


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
    """The Solaris release version, for the sidebar footer. '' if unavailable.

    Prefers the `SOLARIS_VERSION` env injected at image build (the release
    git tag/ref, see build-images.yml) — the package version in pyproject.toml
    is never bumped (releases are git tags, no release-please), so it would
    always read "0.1.0". Falls back to the package metadata for local/dev
    builds where the env is unset, so the badge still shows something.
    """
    import os

    env = os.environ.get("SOLARIS_VERSION", "").strip()
    if env:
        return env
    try:
        from importlib.metadata import version

        return version("solaris-chat")
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
    hermes: EngineClient | Any,
    hermes_admin: EngineClient | Any = None,
    hermes_deep: EngineClient | Any = None,
    hermes_guest: EngineClient | Any = None,
    remote_user_header: str,
    default_uid: str,
    remote_groups_header: str = "Remote-Groups",
    admin_group: str = "admins",
    skills_dir: str = "/data/skills",
    soul_path: str = "/data/SOUL.md",
    logout_url: str = "",
    context_window: ContextWindow | int = STATIC_DEFAULT,
    compaction_threshold: float = compaction.DEFAULT_THRESHOLD,
    attachments_dir: str = "/data/attachments",
    frame_ancestors: str = "'self'",
    fast_model: str = "",
    thorough_model: str = "",
    tts_voices: str = "martin",
    solaris_db_path: str = "/var/lib/solaris/solaris.db",
    notes_dir: str = "/opt/data/notes",
    ollama_url: str = "http://127.0.0.1:11434",
    trace_recorder: Any = None,
    residents: list[str] | None = None,
    api_key: str = "",
    bus: Any = None,
    sb_mcp_url: str = "",
    sb_mcp_token_path: str = "",
    hass_url: str = "",
    hass_token: str = "",
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

    # Profile routing: household sessions ride `hermes` (the engine's
    # household profile); admin/servicebay-maintenance sessions ride the
    # admin profile. A session created on the admin profile is recorded here
    # so its follow-up turns route back to the same profile. When no admin
    # client is configured both fall back to `hermes` (offline-test topology).
    household_gw = hermes
    admin_gw = hermes_admin or hermes
    deep_gw = hermes_deep or hermes
    admin_sessions: set[str] = set()
    deep_sessions: set[str] = set()
    # Sessions pinned to the household (fast e2b) gateway — the pinned "Zuhause"
    # chat. Populated at create (like deep_sessions); the persisted primary topic
    # is the restart-survival source of truth, this set is just the fast path.
    household_sessions: set[str] = set()

    # The everyday-chat model preference (#332-followup): "fast" (e2b household
    # gateway) or "thorough" (12b solaris-deep gateway). Cached in memory; the JSON
    # sidecar beside solaris.db survives restarts. Household chats ignore it.
    other_model_pref = settings_store.get_other_model_pref(solaris_db_path)

    # The one shared "Zuhause" every resident opens (#649): with speaker-ID off
    # all voice is anonymous-household, so spoken and typed turns are one family
    # conversation in a single deterministic row (owner_uid = default_uid).
    shared_household_id = store.household_session_id(default_uid)

    def effective_uid(uid: str, session_id: str) -> str:
        """Admit any authenticated resident to the shared household row (#649).

        Owner-scoped reads/writes filter on `owner_uid`; the shared Zuhause is
        owned by `default_uid`, so a resident's real uid would 403/404 against
        it. Map their uid to the owner for that one session, leave every other
        session per-resident (privacy posture unchanged)."""
        return default_uid if session_id == shared_household_id else uid

    def is_household_chat(uid: str, session_id: str, topic_slug: str) -> bool:
        """True when this turn belongs to the pinned household chat — by the
        first-turn topic, the fast-path set, or the persisted primary topic."""
        if topic_slug == HOUSEHOLD_TOPIC:
            return True
        if session_id and session_id == store.household_session_id(uid):
            # The durable voice/household session (#345) — fast e2b, no think.
            return True
        if session_id and session_id in household_sessions:
            return True
        if session_id:
            assigned = topics_store.get_session_topics(solaris_db_path, session_id, uid)
            return assigned.get("primary") == HOUSEHOLD_TOPIC
        return False

    def gateway_for(
        request: web.Request,
        session_id: str,
        persona: object = None,
        *,
        uid: str = "",
        topic_slug: str = "",
    ) -> EngineClient:
        """Pick the Hermes gateway for a turn (#293/#332/#332-followup).

        The pinned household ("Zuhause") chat is ALWAYS the fast e2b household
        gateway, regardless of the everyday-chat model preference — it is the
        one chat pinned to one model at one strength.

        Every OTHER chat follows the everyday-chat model preference (the Model
        setting): "thorough" routes to the solaris-deep gateway (12b), "fast" to the
        household gateway (e2b). Both carry the same Solaris soul; the model is fixed
        by which gateway the turn lands on (a per-session override is ignored,
        #293). An explicit solaris-deep persona / recorded deep session also routes
        to 12b.

        Admin gateway only when the caller is an Authelia admin AND either the
        session was created on the admin gateway (recorded at create) or this
        request explicitly selects the admin/maintenance persona. A non-admin
        caller is ALWAYS routed off the admin gateway — even if it presents a
        known admin session_id — so the #209/#229 gate holds at the routing
        layer too.
        """
        if is_household_chat(uid, session_id, topic_slug):
            return household_gw
        sel = request.rel_url.query.get("persona") or persona
        if is_admin(request, remote_groups_header, admin_group):
            if session_id and session_id in admin_sessions:
                return admin_gw
            if sel == personalities.MAINTENANCE_ID:
                return admin_gw
        if (session_id and session_id in deep_sessions) or sel == personalities.DEEP_ID:
            return deep_gw
        return deep_gw if other_model_pref == "thorough" else household_gw

    async def maybe_compact(
        uid: str, session_id: str, client: EngineClient
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

        The durable household session (#345) is never forked: a `Fortsetzung`
        continuation would surface as a second "Zuhause" row, defeating the one
        durable session (#419). It stays in-place — its history grows; the
        overnight compactor still extracts learnings without continuation.
        """
        if session_id == store.household_session_id(uid):
            return session_id, False
        try:
            new_id = await compaction.compact_session(
                client,
                uid,
                session_id,
                context_window=context_window.value,
                threshold=compaction_threshold,
            )
        except EngineError:
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
        client: EngineClient,
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
        if primary == HOUSEHOLD_TOPIC:
            # The pinned "Zuhause" first turn lands in the ONE shared household
            # session (#345/#419/#649) — the same row voice turns use — so it
            # never mints a fresh session per click/first-turn, and every
            # resident opens the same conversation. Owned by default_uid.
            session_id = store.ensure_household_session(solaris_db_path, default_uid)
            household_sessions.add(session_id)
            # Stamp the household primary topic so the list chip + pinned-row
            # highlight (primary_topic == household) light up for this row (#241).
            topics_store.set_primary(
                solaris_db_path, session_id, HOUSEHOLD_TOPIC, default_uid
            )
            log.info(
                "chat.session.created",
                uid=uid,
                session_id=session_id,
                topic=HOUSEHOLD_TOPIC,
            )
            return session_id
        # Born with a unique marker-embedded title (not the bare `[uid:...]`
        # marker), so a first turn can never 400 against an abandoned
        # bare-marker stub already holding it — the same collision #267 fixed
        # for the compaction path, here on the main first-turn path (#277).
        session_id = await client.create_session(uid, title=_title_from(text))
        if client is admin_gw and client is not household_gw:
            admin_sessions.add(session_id)
        elif client is deep_gw and client is not household_gw:
            deep_sessions.add(session_id)
        log.info(
            "chat.session.created",
            uid=uid,
            session_id=session_id,
            topic=primary or "",
        )
        if primary:
            topics_store.set_primary(solaris_db_path, session_id, primary, uid)
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
                display = topics_store.display_name(solaris_db_path, extract_topic)
                label = display or extract_topic
                parts.append(
                    f"[Extract this to a note #topic/{extract_topic} ({label})]"
                )
            parts.append(text)
            return "\n\n".join(parts)
        hint = topics_store.topic_context_hint(solaris_db_path, session_id, uid)
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
        mentions_store.record_mentions(solaris_db_path, session_id, uid, tags, persons)

    def record_anchors(
        uid: str,
        session_id: str,
        anchors: list[str],
        user_text: str,
        *,
        ephemeral: bool,
    ) -> None:
        """Record the agent's auto-surfaced #/@ anchors as turn mentions (#501).

        Anchors keep their `#`/`@` prefix; split into tags/persons and dedup
        against the tokens the user already typed this turn (those are recorded
        by persist_mentions). Skipped for ephemeral chats; no-op when empty.
        """
        if ephemeral or not anchors:
            return
        typed_tags, typed_persons = parse_mentions(user_text)
        typed = {("#", t) for t in typed_tags} | {("@", p) for p in typed_persons}
        tags, persons = [], []
        for a in anchors:
            prefix, value = a[:1], a[1:].strip().lower()
            if not value or (prefix, value) in typed:
                continue
            (persons if prefix == "@" else tags).append(value)
        mentions_store.record_mentions(solaris_db_path, session_id, uid, tags, persons)

    async def persist_turn_trace(
        uid: str,
        session_id: str,
        t0: float,
        *,
        ephemeral: bool,
        ha_cards: list[dict[str, Any]] | None = None,
        suggestions: list[str] | None = None,
        anchors: list[str] | None = None,
    ) -> None:
        """Persist this turn's engine trace steps under a fresh trace_id.

        Native engine tracing: records carry the session id, so the turn's
        steps are an exact filter (`t0` bounds them to this turn) — no
        time-window guessing. Skipped for ephemeral chats (no durable state);
        fail-open — a DB hiccup never breaks the turn that already produced a
        reply.
        """
        if ephemeral or trace_recorder is None:
            return
        try:
            trace_id = uuid.uuid4().hex
            steps = []
            for order, rec in enumerate(trace_recorder.for_session(session_id, t0)):
                # Persist the detail body WITH the step (#451) under a stable
                # per-step key, so the modal still resolves after a reload /
                # engine restart — the recorder's ring id restarts at 0 per
                # process and would otherwise 404. Tool steps carry no body.
                detail = trace_recorder.detail(rec["id"]) if "id" in rec else None
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
            # The turn's read-only HA cards (#475) ride the same trace_id as a
            # synthetic step, so the frontend re-attaches them to this turn's
            # bubble on reload alongside the step trace. detail_json carries the
            # card-specs; step_kind tags it so reload can pick it out.
            if ha_cards:
                steps.append(
                    {
                        "step_kind": "ha_cards",
                        "detail_json": json.dumps(ha_cards),
                    }
                )
            # Follow-up chips (#498) ride the same trace_id as a synthetic step,
            # so reload re-attaches them under the turn's bubble (like ha_cards).
            if suggestions:
                steps.append(
                    {
                        "step_kind": "suggestions",
                        "detail_json": json.dumps(suggestions),
                    }
                )
            # Auto-surfaced #/@ anchors (#501) ride the same trace_id as a
            # synthetic step, so reload re-attaches the chips under the bubble.
            if anchors:
                steps.append(
                    {
                        "step_kind": "anchors",
                        "detail_json": json.dumps(anchors),
                    }
                )
            if steps:
                trace_store.persist_trace(
                    solaris_db_path, session_id, trace_id, uid, steps
                )
        except Exception as e:  # noqa: BLE001 — trace persistence is best-effort
            log.warn("chat.trace.persist_error", session_id=session_id, error=str(e))

    async def session_trace(request: web.Request) -> web.Response:
        # The persisted per-turn LLM trace for one chat (#306): the ordered steps
        # the proxy captured, each with model/wall_s/tokens/detail_id, so the
        # panel renders the same trace on reopen. Per-resident scope.
        uid = resolve_uid(request, remote_user_header, default_uid)
        session_id = request.match_info["session_id"]
        steps = trace_store.list_session_trace(
            solaris_db_path, session_id, effective_uid(uid, session_id)
        )
        return web.json_response({"ok": True, "steps": steps})

    async def session_events(request: web.Request) -> web.StreamResponse:
        # The live mirror (#344): a browser opens this for the session it's
        # showing and receives turns that originate elsewhere — voice via the
        # facade, or another tab of the same person — near-live. Per-resident
        # scope: only the session's owner uid sees its turns (privacy posture,
        # like trace_store D3), so a wrong-owner subscribe gets a silent empty
        # stream. The originating request keeps its own /api/chat/stream; this
        # only carries the OTHER clients' view.
        uid = effective_uid(
            resolve_uid(request, remote_user_header, default_uid),
            request.match_info["session_id"],
        )
        session_id = request.match_info["session_id"]
        if store.session_owner(solaris_db_path, session_id) != uid:
            return web.json_response({"ok": False, "reason": "forbidden"}, status=403)
        resp = web.StreamResponse(
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            }
        )
        await resp.prepare(request)
        if bus is None:
            await _send_event(resp, "done", {})
            return resp
        streamed = False
        try:
            async for item in bus.subscribe(session_id, uid):
                kind = item.get("kind")
                if kind == "mirror_user":
                    text = strip_internal_hints(str(item["event"].get("text") or ""))
                    await _send_event(resp, "mirror_user", {"text": text})
                    streamed = False
                elif kind == "mirror_event":
                    name, data = _normalize(item["event"])
                    if name == "delta" and data.get("text"):
                        streamed = True
                    elif name == "completed":
                        # A tool-only turn streams no deltas — surface the final
                        # answer once (the #258 late-delta pattern), but don't
                        # double it when the answer already streamed.
                        answer = data.pop("answer", "")
                        if answer and not streamed:
                            await _send_event(resp, "delta", {"text": answer})
                    await _send_event(resp, name, data)
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        return resp

    async def trace_detail(request: web.Request) -> web.Response:
        # Exact per-call content for one trace step (#307 panel → #305 detail).
        # A persisted turn carries a stable `<trace_id>:<order>` detail_id whose
        # body lives in the trace store (#451), so it survives a reload/restart;
        # the in-flight turn still carries the recorder's bare integer ring id,
        # served live. Try the persisted body first, then the ring.
        uid = resolve_uid(request, remote_user_header, default_uid)
        detail_id = request.match_info["detail_id"]
        body = trace_store.detail_for(solaris_db_path, uid, detail_id)
        if body is not None:
            return web.Response(body=body, content_type="application/json")
        detail = (
            trace_recorder.detail(int(detail_id))
            if trace_recorder is not None and detail_id.isdigit()
            else None
        )
        if detail is None:
            return web.json_response({"error": "not found"}, status=404)
        return web.json_response(detail)

    async def index(_request: web.Request) -> web.Response:
        # `no-cache` = the browser may cache but MUST revalidate (ETag/
        # Last-Modified, set by FileResponse) before serving. Without it the
        # HTML shell — which carries all the inline JS/CSS — is heuristically
        # cached, so mobile keeps showing a stale UI and deploys don't land.
        return web.FileResponse(
            STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"}
        )

    async def health(_request: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    async def ha_call(request: web.Request) -> web.Response:
        """Card-action endpoint (#476): run a scoped HA service on one entity.

        Phase 2 surfaced toggles on light/switch; phase 3 (#477) adds the slider/
        colour/climate controls, so cover and climate cards may act too. The
        helper applies the same allowlist as the `ha_call_service` tool (blocked
        domains, name regex, domain==entity). Owner-scoped: any authenticated
        resident, no client-side HA token. Returns the new state to confirm.
        """
        if not hass_url or not hass_token:
            return web.json_response(
                {"ok": False, "error": "ha_not_configured"}, status=503
            )
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"ok": False, "error": "bad_json"}, status=400)
        entity_id = str(body.get("entity_id") or "")
        service = str(body.get("service") or "")
        if entity_id.split(".", 1)[0] not in ("light", "switch", "cover", "climate"):
            return web.json_response(
                {"ok": False, "error": "unsupported_domain"}, status=400
            )
        data = body.get("data")
        result = await call_service_scoped(
            hass_url,
            hass_token,
            entity_id,
            service,
            data if isinstance(data, dict) else None,
        )
        return web.json_response(result, status=200 if result.get("ok") else 400)

    async def whoami(request: web.Request) -> web.Response:
        uid = resolve_uid(request, remote_user_header, default_uid)
        return web.json_response(
            {
                "ok": True,
                "uid": uid,
                "is_admin": is_admin(request, remote_groups_header, admin_group),
                "version": VERSION,
                "logout_url": logout_url,
                "context_window": context_window.value,
                # The ONE shared household session every resident opens (#649):
                # the pinned "Zuhause" row opens this id so spoken (anonymous-
                # household) and typed turns are the same conversation, visible
                # to every logged-in resident instead of split per-uid.
                "household_session_id": shared_household_id,
            }
        )

    async def list_toolsets(_request: web.Request) -> web.Response:
        try:
            toolsets = await hermes.list_toolsets()
        except EngineError:
            return web.json_response(
                {"ok": False, "reason": "engine_unavailable"}, status=502
            )
        return web.json_response({"ok": True, "toolsets": toolsets})

    def _admin_mcp() -> McpToolbox | None:
        """The admin profile's ServiceBay MCP toolbox, when one is wired.

        Since #386 the admin toolbox may be a `CombinedToolbox` wrapping the
        McpToolbox alongside local onboarding tools — find the McpToolbox in
        either shape."""
        toolbox = getattr(getattr(admin_gw, "_profile", None), "toolbox", None)
        if isinstance(toolbox, McpToolbox):
            return toolbox
        for box in getattr(toolbox, "_boxes", []):
            if isinstance(box, McpToolbox):
                return box
        return None

    async def list_mcp(_request: web.Request) -> web.Response:
        # The engine's MCP surface is the admin profile's servicebay_admin
        # toolbox — report it (name/url/reachable/tools, no tokens).
        mcp = _admin_mcp()
        if mcp is None:
            return web.json_response({"ok": True, "servers": []})
        await mcp.prepare()
        names = mcp.names()
        return web.json_response(
            {
                "ok": True,
                "servers": [
                    {
                        "name": "servicebay_admin",
                        "url": mcp.url,
                        "reachable": bool(names),
                        "tools": names,
                    }
                ],
            }
        )

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
        mcp = _admin_mcp()
        if mcp is None or request.match_info["server"] != "servicebay_admin":
            return web.json_response({"ok": False, "error": "Unknown MCP server"})
        await mcp.prepare()
        output = await mcp.dispatch(tool.strip(), arguments)
        log.info(
            "chat.mcp.test",
            uid=resolve_uid(request, remote_user_header, default_uid),
            server=request.match_info["server"],
            tool=tool.strip(),
        )
        return web.json_response({"ok": True, "result": output})

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

    def _valid_kind(request: web.Request) -> str | None:
        kind = request.match_info["kind"]
        return kind if kind in skills.KINDS else None

    async def list_defs(request: web.Request) -> web.Response:
        kind = _valid_kind(request)
        if kind is None:
            return web.json_response(
                {"ok": False, "reason": "unknown_kind"}, status=404
            )
        return web.json_response(
            {"ok": True, "kind": kind, "defs": skills.list_defs(skills_dir, kind)}
        )

    async def get_def(request: web.Request) -> web.Response:
        kind = _valid_kind(request)
        if kind is None:
            return web.json_response(
                {"ok": False, "reason": "unknown_kind"}, status=404
            )
        one = skills.read_def(skills_dir, kind, request.match_info["def_id"])
        if one is None:
            return web.json_response({"ok": False, "reason": "not_found"}, status=404)
        return web.json_response({"ok": True, "def": one})

    async def put_def(request: web.Request) -> web.Response:
        if not is_admin(request, remote_groups_header, admin_group):
            return web.json_response({"ok": False, "reason": "forbidden"}, status=403)
        kind = _valid_kind(request)
        if kind is None:
            return web.json_response(
                {"ok": False, "reason": "unknown_kind"}, status=404
            )
        def_id = request.match_info["def_id"]
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
            result = skills.write_def(skills_dir, kind, def_id, content)
        except OSError:
            return web.json_response(
                {"ok": False, "reason": "write_failed"}, status=500
            )
        if result is None:
            # Bad id, or the content's kind contradicts the registry.
            return web.json_response(
                {"ok": False, "reason": "kind_mismatch"}, status=400
            )
        log.info(
            "chat.def.edited",
            uid=resolve_uid(request, remote_user_header, default_uid),
            kind=kind,
            def_id=def_id,
            created=result["created"],
            frontmatter_changed=result["frontmatter_changed"],
        )
        return web.json_response(
            {
                "ok": True,
                "created": result["created"],
                "restart_needed": result["frontmatter_changed"],
            }
        )

    async def delete_def_route(request: web.Request) -> web.Response:
        if not is_admin(request, remote_groups_header, admin_group):
            return web.json_response({"ok": False, "reason": "forbidden"}, status=403)
        kind = _valid_kind(request)
        if kind is None:
            return web.json_response(
                {"ok": False, "reason": "unknown_kind"}, status=404
            )
        def_id = request.match_info["def_id"]
        if not skills.delete_def(skills_dir, kind, def_id):
            return web.json_response({"ok": False, "reason": "not_found"}, status=404)
        log.info(
            "chat.def.deleted",
            uid=resolve_uid(request, remote_user_header, default_uid),
            kind=kind,
            def_id=def_id,
        )
        return web.json_response({"ok": True})

    async def get_soul(_request: web.Request) -> web.Response:
        # The soul lives on the chat-owned data volume now (Solaris Engine reads
        # it per turn), so the panel reads the file directly — the Hermes-era
        # config-sidecar hop is gone.
        try:
            content = Path(soul_path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return web.json_response(
                {"ok": False, "reason": "soul_unavailable"}, status=502
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
        # Atomic write on the chat-owned volume: the engine's mtime cache
        # picks the edit up on the next turn, so it is live without restart.
        try:
            tmp = Path(soul_path).with_suffix(".tmp")
            tmp.write_text(content, encoding="utf-8")
            tmp.replace(soul_path)
        except OSError as e:
            log.error("chat.soul.write_failed", error=str(e))
            return web.json_response(
                {"ok": False, "reason": "soul_unavailable"}, status=502
            )
        log.info(
            "chat.soul.edited",
            uid=resolve_uid(request, remote_user_header, default_uid),
        )
        return web.json_response({"ok": True})

    # The household model the picker offers (#366): the configured FAST_MODEL
    # default plus the thorough model, so an admin can put the bigger model on
    # the household hot path. The persisted override ("" = use the default) is
    # read here so GET reflects the live selection.
    def household_model_options() -> list[dict[str, str]]:
        opts = [{"value": fast_model, "model": fast_model}]
        if thorough_model and thorough_model != fast_model:
            opts.append({"value": thorough_model, "model": thorough_model})
        return opts

    def current_household_model() -> str:
        return settings_store.get_household_model(solaris_db_path) or fast_model

    async def get_model(request: web.Request) -> web.Response:
        # Admin-only: the everyday-chat model toggle is an admin control.
        if not is_admin(request, remote_groups_header, admin_group):
            return web.json_response({"ok": False, "reason": "forbidden"}, status=403)
        return web.json_response(
            {
                "ok": True,
                "current": other_model_pref,
                "options": [
                    {"value": "fast", "label": "Schnell", "model": fast_model},
                    {
                        "value": "thorough",
                        "label": "Gründlich",
                        "model": thorough_model,
                    },
                ],
                "household_current": current_household_model(),
                "household_default": fast_model,
                "household_options": household_model_options(),
            }
        )

    async def put_model(request: web.Request) -> web.Response:
        nonlocal other_model_pref
        if not is_admin(request, remote_groups_header, admin_group):
            return web.json_response({"ok": False, "reason": "forbidden"}, status=403)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 — any malformed JSON
            return web.json_response(
                {"ok": False, "reason": "invalid_json"}, status=400
            )
        # The household-profile model override (#366): a separate field so this
        # one endpoint sets either the everyday-chat routing toggle or the
        # household model. The selection must be one of the offered tags (the
        # picker only shows those). Persists; the household profile reads it on
        # the next turn — no restart.
        household_value = body.get("household_model")
        if household_value is not None:
            valid = {o["value"] for o in household_model_options()}
            if household_value not in valid:
                return web.json_response(
                    {"ok": False, "reason": "invalid_value"}, status=400
                )
            settings_store.set_household_model(solaris_db_path, household_value)
            log.info(
                "chat.model.household.set",
                uid=resolve_uid(request, remote_user_header, default_uid),
                model=household_value,
            )
            return web.json_response({"ok": True, "household_current": household_value})
        value = body.get("value")
        if value not in ("fast", "thorough"):
            return web.json_response(
                {"ok": False, "reason": "invalid_value"}, status=400
            )
        # A routing toggle, not a Hermes config rewrite: the household chat stays
        # on e2b; every OTHER chat routes to e2b ("fast") or the 12b solaris-deep
        # gateway ("thorough"). Takes effect on the next turn — no restart.
        other_model_pref = value
        settings_store.set_other_model_pref(solaris_db_path, value)
        log.info(
            "chat.model.set",
            uid=resolve_uid(request, remote_user_header, default_uid),
            pref=value,
        )
        return web.json_response({"ok": True, "current": value})

    # The global TTS voice picker (#368): one Kokoro voice for all spoken
    # output, mirroring the household-model picker. The offered voices come from
    # TTS_VOICES (the box's solaris-tts image declares which it ships); the
    # first is the default. The persisted "" means "use the default", so an
    # untouched install keeps the baked-in Martin voice. The post-deploy reads
    # the persisted value and converges the Assist pipeline's tts_voice.
    def voice_options() -> list[str]:
        return [v.strip() for v in tts_voices.split(",") if v.strip()]

    def default_voice() -> str:
        opts = voice_options()
        return opts[0] if opts else ""

    def current_voice() -> str:
        return settings_store.get_tts_voice(solaris_db_path) or default_voice()

    async def get_voice(request: web.Request) -> web.Response:
        if not is_admin(request, remote_groups_header, admin_group):
            return web.json_response({"ok": False, "reason": "forbidden"}, status=403)
        return web.json_response(
            {
                "ok": True,
                "current": current_voice(),
                "default": default_voice(),
                "options": voice_options(),
            }
        )

    async def put_voice(request: web.Request) -> web.Response:
        if not is_admin(request, remote_groups_header, admin_group):
            return web.json_response({"ok": False, "reason": "forbidden"}, status=403)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 — any malformed JSON
            return web.json_response(
                {"ok": False, "reason": "invalid_json"}, status=400
            )
        value = body.get("value")
        if value not in voice_options():
            return web.json_response(
                {"ok": False, "reason": "invalid_value"}, status=400
            )
        settings_store.set_tts_voice(solaris_db_path, value)
        log.info(
            "chat.voice.set",
            uid=resolve_uid(request, remote_user_header, default_uid),
            voice=value,
        )
        return web.json_response({"ok": True, "current": value})

    # The model tags whose combined VRAM footprint the headroom estimate sums:
    # the household model (selected or fast default), the thorough model the
    # deep/"Gründlich" path runs, and the embedding model — i.e. what's
    # actually meant to be co-resident on the box.
    def selected_models() -> list[str]:
        tags = [current_household_model(), thorough_model]
        embed = os.environ.get("EMBED_MODEL", "").strip()
        if embed:
            tags.append(embed)
        return [t for t in tags if t]

    async def get_vram(request: web.Request) -> web.Response:
        if not is_admin(request, remote_groups_header, admin_group):
            return web.json_response({"ok": False, "reason": "forbidden"}, status=403)
        client = OllamaChat(ollama_url)
        try:
            tags, ps = await client.tags(), await client.ps()
        except Exception:  # noqa: BLE001 — Ollama down => no estimate, not a 500
            tags, ps = [], []
        selected = selected_models()
        combined = vram.combined_selected_bytes(selected, tags, ps)
        # Real total/used from ServiceBay's node agent (its nvidia-smi sees the
        # whole GPU, overhead included) — falls back to env/in-container smi.
        gpu = await vram.servicebay_gpu(sb_mcp_url, sb_mcp_token_path)
        if gpu is not None:
            gpu_total, gpu_used = gpu
            available: int | None = max(gpu_total - gpu_used, 0)
        else:
            gpu_total = gpu_used = None
            available = vram.available_bytes(ps)
        return web.json_response(
            {
                "ok": True,
                "estimate": True,
                "selected": selected,
                "combined_bytes": combined,
                "available_bytes": available,
                "gpu_total_bytes": gpu_total,
                "gpu_used_bytes": gpu_used,
                # available unknown => we can't judge fit, so don't flag.
                "over_budget": available is not None and combined > available,
            }
        )

    async def pull_model(request: web.Request) -> web.StreamResponse:
        if not is_admin(request, remote_groups_header, admin_group):
            return web.json_response({"ok": False, "reason": "forbidden"}, status=403)
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 — any malformed JSON
            return web.json_response(
                {"ok": False, "reason": "invalid_json"}, status=400
            )
        model = (body.get("model") or "").strip()
        if not model:
            return web.json_response({"ok": False, "reason": "no_model"}, status=400)
        log.info(
            "chat.model.pull",
            uid=resolve_uid(request, remote_user_header, default_uid),
            model=model,
        )
        resp = web.StreamResponse()
        resp.content_type = "application/x-ndjson"
        await resp.prepare(request)
        client = OllamaChat(ollama_url)
        try:
            async for chunk in client.pull(model):
                await resp.write((json.dumps(chunk) + "\n").encode())
        except OllamaError as e:
            await resp.write((json.dumps({"error": str(e)}) + "\n").encode())
        await resp.write_eof()
        return resp

    async def list_sessions(request: web.Request) -> web.Response:
        uid = resolve_uid(request, remote_user_header, default_uid)
        try:
            sessions = await hermes.list_sessions(uid)
        except EngineError:
            return web.json_response(
                {"ok": False, "reason": "engine_unavailable"}, status=502
            )
        # Annotate each session with its primary topic so the list can render a
        # chip (#241). Per-resident scope (D3): only the caller's assignments.
        ids = [str(s.get("id")) for s in sessions if s.get("id")]
        primaries = topics_store.primary_topics_for(solaris_db_path, ids, uid)
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
            # The admin profile OWNS the operator soul + skill pack (prompt
            # assembly, Phase 3) — an empty create lets the profile supply it.
            # Any `personality` in the body is ignored; the lock cannot be
            # overridden by the client.
            try:
                session_id = await admin_gw.create_session(uid, maintenance=True)
            except EngineError:
                return web.json_response(
                    {"ok": False, "reason": "engine_unavailable"}, status=502
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
        except EngineError:
            return web.json_response(
                {"ok": False, "reason": "engine_unavailable"}, status=502
            )
        log.info("chat.session.created", uid=uid, session_id=session_id)
        return web.json_response({"ok": True, "session_id": session_id})

    async def get_session(request: web.Request) -> web.Response:
        uid = resolve_uid(request, remote_user_header, default_uid)
        session_id = request.match_info["session_id"]
        try:
            session = await hermes.get_session(
                session_id, effective_uid(uid, session_id)
            )
        except EngineError:
            return web.json_response(
                {"ok": False, "reason": "engine_unavailable"}, status=502
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
        # Owner-scoped (#438): a caller can only delete their own session. A
        # wrong-owner id is indistinguishable from a missing one (like
        # get_session), so a cross-resident delete leaks nothing.
        uid = resolve_uid(request, remote_user_header, default_uid)
        session_id = request.match_info["session_id"]
        try:
            ok = await hermes.delete_session(session_id, uid)
        except EngineError:
            return web.json_response(
                {"ok": False, "reason": "engine_unavailable"}, status=502
            )
        if not ok:
            return web.json_response({"ok": False, "reason": "not_found"}, status=404)
        attachments.delete(session_id)
        log.info("chat.session.deleted", uid=uid, session_id=session_id)
        return web.json_response({"ok": True})

    async def list_topics(request: web.Request) -> web.Response:
        uid = resolve_uid(request, remote_user_header, default_uid)
        return web.json_response(
            {"ok": True, "topics": topics_store.list_topics(solaris_db_path, uid)}
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
        topics_store.create_topic(solaris_db_path, slug, display_name, uid, color)
        log.info("chat.topic.create", uid=uid, slug=slug)
        return web.json_response({"ok": True, "slug": slug})

    async def get_session_topics(request: web.Request) -> web.Response:
        uid = resolve_uid(request, remote_user_header, default_uid)
        session_id = request.match_info["session_id"]
        assigned = topics_store.get_session_topics(solaris_db_path, session_id, uid)
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
            topics_store.set_primary(solaris_db_path, session_id, slug, uid)
        elif action == "add_secondary":
            topics_store.add_secondary(solaris_db_path, session_id, slug, uid)
        elif action == "remove":
            topics_store.remove_topic(solaris_db_path, session_id, slug, uid)
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
        assigned = topics_store.get_session_topics(solaris_db_path, session_id, uid)
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

    async def concept_view(request: web.Request) -> web.Response:
        """Aggregate one entity/concept into the #502 page (phase 1).

        Composes, owner-scoped, what the household already stores for `<id>`:
        live HA state (when the id is an HA entity), the OKF concept's
        description/facts/events, the source OKF document + notes that mention
        it, and chat/note backlinks. `<id>` resolves via `entity_aliases`/
        `entities` (migration 0016) or is taken as an HA entity id directly.
        """
        uid = resolve_uid(request, remote_user_header, default_uid)
        ref = request.match_info["id"].strip()
        view: dict[str, Any] = {
            "id": ref,
            "title": ref,
            "type": None,
            "ha_card": None,
            "description": "",
            "body": "",
            "facts": [],
            "events": [],
            "source_docs": [],
            "backlinks": [],
        }
        names = [ref]
        if Path(solaris_db_path).exists():
            conn = projection.open_conn(solaris_db_path)
            try:
                entity_id = projection.resolve_entity_id(conn, ref, uid)
                if entity_id is not None:
                    ent = projection.entity_row(conn, entity_id) or {}
                    view["id"] = entity_id
                    view["title"] = ent.get("canonical_name") or entity_id
                    view["type"] = ent.get("type")
                    names = [view["title"], *projection.entity_aliases(conn, entity_id)]
                    view["facts"] = projection.entity_facts(conn, entity_id, uid)
                    view["events"] = projection.entity_events(conn, entity_id, uid)
                    okf_path = projection.entity_okf_path(conn, entity_id)
                    if okf_path:
                        view["source_docs"].append(
                            {"path": okf_path, "title": view["title"], "kind": "okf"}
                        )
                        parsed = _read_okf(notes_dir, okf_path)
                        view["description"] = parsed["description"]
                        view["body"] = parsed["body"]
            except sqlite3.OperationalError:
                pass
            finally:
                conn.close()
        # Live state when the page id (or the resolved entity) is an HA entity.
        if hass_url and hass_token:
            card = await fetch_card(hass_url, hass_token, ref)
            if card is not None:
                view["ha_card"] = card
                if view["title"] == ref:
                    view["title"] = card.get("name") or ref
        for note in notes_search.notes_mentioning(notes_dir, names, uid):
            view["source_docs"].append({**note, "kind": "note"})
        # Backlinks span both surfaces (#505): chat turns that referenced the
        # concept and vault notes whose `[[ ]]` link targets its okf/ file.
        view["backlinks"] = mentions_store.backlinks_for(solaris_db_path, uid, names)
        okf_path = next(
            (d["path"] for d in view["source_docs"] if d["kind"] == "okf"), None
        )
        for note in notes_search.notes_wikilinking(notes_dir, names, okf_path, uid):
            view["backlinks"].append({**note, "kind": "note"})
        return web.json_response({"ok": True, "concept": view})

    async def concept_page(_request: web.Request) -> web.Response:
        # Bookmarkable deep-link to a concept: serve the SPA shell; the client
        # router reads the `/c/<id>` path and renders the page from the API.
        return web.FileResponse(
            STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"}
        )

    async def portal_energy(_request: web.Request) -> web.Response:
        """Aggregate the home-energy picture for the `#/p/energy` page (#503).

        Same pattern as `/api/concept/<id>`: a read-only `/api` aggregator behind
        the existing Authelia gate, rendered by an SPA view. 503 when HA is not
        configured for this household.
        """
        if not hass_url or not hass_token:
            return web.json_response(
                {"ok": False, "error": "ha_unconfigured"}, status=503
            )
        energy = await fetch_energy(hass_url, hass_token)
        if energy is None:
            return web.json_response(
                {"ok": False, "error": "ha_unavailable"}, status=502
            )
        return web.json_response({"ok": True, "energy": energy})

    async def portal_energy_history(request: web.Request) -> web.Response:
        """Power history for the energy page's 24h/7d trend chart (#689).

        Same read-only Authelia-gated pattern as `portal_energy`; `range=24h|7d`
        selects the window (default 24h). 503 when HA is unconfigured, 502 when
        it is unavailable.
        """
        if not hass_url or not hass_token:
            return web.json_response(
                {"ok": False, "error": "ha_unconfigured"}, status=503
            )
        hours = 168 if request.query.get("range") == "7d" else 24
        history = await fetch_energy_history(hass_url, hass_token, hours)
        if history is None:
            return web.json_response(
                {"ok": False, "error": "ha_unavailable"}, status=502
            )
        return web.json_response({"ok": True, "history": history})

    async def portal_page(_request: web.Request) -> web.Response:
        # Bookmarkable deep-link to a household page: serve the SPA shell; the
        # client router reads the `/p/<type>` path and renders from the API.
        return web.FileResponse(
            STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"}
        )

    # Room data for the start-page picker (#669): a long-lived registry so the
    # WS snapshot is TTL-cached across picker opens (same source as the engine's
    # `list_rooms`). Only built when HA is configured.
    area_registry = (
        AreaRegistry(hass_url, hass_token) if hass_url and hass_token else None
    )

    def _action_is_sensitive(payload: dict[str, Any]) -> bool:
        """Re-check whether a pinned action would be confirm-gated (#646).

        The run path bypasses the agent-loop gate, so it must re-classify here.
        Only `ha_call_service` can be sensitive; the server holds no
        EntityRegistry, so a cover passes device_class=None and `is_sensitive`
        fails SAFE (gated) — such a favorite could never be pinned by #645
        anyway, so this is pure defense-in-depth."""
        if payload.get("tool") != "ha_call_service":
            return False
        args = payload.get("args") if isinstance(payload.get("args"), dict) else {}
        domain = str(args.get("domain") or "")
        service = str(args.get("service") or "")
        service = _SERVICE_ALIASES.get(domain, {}).get(service, service)
        return confirm.is_sensitive(domain, service, None)

    async def portal_start(request: web.Request) -> web.Response:
        """Aggregate the resident's start page (#646): their pins + the shared
        household ones + their most-used actions. Entity favorites are enriched
        with live HA state via the read-only `fetch_card` path."""
        uid = resolve_uid(request, remote_user_header, default_uid)
        favorites = favorites_store.list_favorites(solaris_db_path, uid)
        personal: list[dict[str, Any]] = []
        household: list[dict[str, Any]] = []

        async def enrich(fav: dict[str, Any]) -> dict[str, Any]:
            item = {
                "id": fav["id"],
                "kind": fav["kind"],
                "label": fav["label"],
                "payload": fav["payload"],
                "position": fav["position"],
                "scope": (
                    "household"
                    if fav["owner_uid"] == favorites_store.HOUSEHOLD
                    else "personal"
                ),
            }
            if fav["kind"] == "entity" and hass_url and hass_token:
                entity_id = str(fav["payload"].get("entity_id") or "")
                item["card"] = (
                    await fetch_card(hass_url, hass_token, entity_id)
                    if entity_id
                    else None
                )
            return item

        enriched = await asyncio.gather(*(enrich(f) for f in favorites))
        for item in enriched:
            (household if item["scope"] == "household" else personal).append(item)

        frequent: list[dict[str, Any]] = []
        for row in favorites_store.top_usage(solaris_db_path, uid, 6):
            frequent.append(
                {"kind": row["kind"], "label": _favorite_label(row["payload"]), **row}
            )
        return web.json_response(
            {
                "ok": True,
                "personal": personal,
                "household": household,
                "frequent": frequent,
            }
        )

    async def portal_start_addable(request: web.Request) -> web.Response:
        """Addable entity cards for the start-page picker, grouped by room (#669).

        The resident opens the picker in edit mode and ticks cards to pin. This
        lists the house's controllable actuators (reusing the room-card domain
        set + live state), grouped by room, marking those already pinned
        (personal or household) so they aren't offered twice. A cover whose
        one-tap toggle would be confirm-gated (garage/door/gate) is marked
        `sensitive` — the client renders it disabled, mirroring the pin-time
        refusal the favorites store enforces for actions."""
        if not hass_url or not hass_token or area_registry is None:
            return web.json_response(
                {"ok": False, "error": "ha_unconfigured"}, status=503
            )
        uid = resolve_uid(request, remote_user_header, default_uid)
        snap = await area_registry.snapshot()
        cards = await fetch_addable_cards(hass_url, hass_token, snap.entity_area)
        if cards is None:
            return web.json_response(
                {"ok": False, "error": "ha_unavailable"}, status=502
            )
        pinned = {
            str(f["payload"].get("entity_id") or "")
            for f in favorites_store.list_favorites(solaris_db_path, uid)
            if f["kind"] == "entity"
        }
        rooms: dict[str, list[dict[str, Any]]] = {}
        for card in cards:
            eid = str(card.get("entity_id") or "")
            card["pinned"] = eid in pinned
            card["sensitive"] = card.get("domain") == "cover" and confirm.is_sensitive(
                "cover", "toggle", card.get("device_class")
            )
            rooms.setdefault(card.pop("room") or "", []).append(card)
        grouped = [
            {"room": room, "cards": rooms[room]}
            for room in sorted(rooms, key=lambda r: (r == "", r.lower()))
        ]
        return web.json_response({"ok": True, "rooms": grouped})

    async def favorites_create(request: web.Request) -> web.Response:
        uid = resolve_uid(request, remote_user_header, default_uid)
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"ok": False, "error": "bad_json"}, status=400)
        kind = str(body.get("kind") or "")
        label = str(body.get("label") or "")
        payload = body.get("payload")
        if kind not in ("entity", "action", "link") or not isinstance(payload, dict):
            return web.json_response({"ok": False, "error": "bad_request"}, status=400)
        owner = favorites_store.HOUSEHOLD if body.get("scope") == "household" else uid
        if kind == "action":
            if payload.get("tool") not in PINNABLE_TOOLS:
                return web.json_response(
                    {"ok": False, "error": "tool_not_pinnable"}, status=403
                )
            if _action_is_sensitive(payload):
                return web.json_response(
                    {"ok": False, "error": "sensitive_action"}, status=403
                )
        fav_id = favorites_store.add_favorite(
            solaris_db_path, owner, kind, label, payload
        )
        return web.json_response({"ok": True, "id": fav_id})

    def _owner_for(request: web.Request, fav_id: str) -> str | None:
        """The owner_uid a resident may mutate this favorite under: their own
        uid or `household`, whichever holds the row. None → not found/forbidden."""
        uid = resolve_uid(request, remote_user_header, default_uid)
        for fav in favorites_store.list_favorites(solaris_db_path, uid):
            if fav["id"] == fav_id:
                return fav["owner_uid"]
        return None

    async def favorites_delete(request: web.Request) -> web.Response:
        fav_id = request.match_info["fav_id"]
        owner = _owner_for(request, fav_id)
        if owner is None:
            return web.json_response({"ok": False, "error": "not_found"}, status=404)
        favorites_store.remove_favorite(solaris_db_path, owner, fav_id)
        return web.json_response({"ok": True})

    async def favorites_reorder(request: web.Request) -> web.Response:
        fav_id = request.match_info["fav_id"]
        owner = _owner_for(request, fav_id)
        if owner is None:
            return web.json_response({"ok": False, "error": "not_found"}, status=404)
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return web.json_response({"ok": False, "error": "bad_json"}, status=400)
        try:
            position = int(body.get("position"))
        except (TypeError, ValueError):
            return web.json_response({"ok": False, "error": "bad_request"}, status=400)
        favorites_store.set_position(solaris_db_path, owner, fav_id, position)
        return web.json_response({"ok": True})

    async def favorites_run(request: web.Request) -> web.Response:
        """Execute a pinned action favorite verbatim (#646). Re-checks the
        confirm policy — a one-tap start-page bypass of the gate must not
        exist — then dispatches on the household gateway toolbox."""
        uid = resolve_uid(request, remote_user_header, default_uid)
        fav_id = request.match_info["fav_id"]
        row = next(
            (
                f
                for f in favorites_store.list_favorites(solaris_db_path, uid)
                if f["id"] == fav_id
            ),
            None,
        )
        if row is None:
            return web.json_response({"ok": False, "error": "not_found"}, status=404)
        if row["kind"] != "action":
            return web.json_response({"ok": False, "error": "not_action"}, status=400)
        payload = row["payload"]
        tool = str(payload.get("tool") or "")
        args = payload.get("args") if isinstance(payload.get("args"), dict) else {}
        if tool not in PINNABLE_TOOLS:
            return web.json_response(
                {"ok": False, "error": "tool_not_pinnable"}, status=403
            )
        if _action_is_sensitive(payload):
            return web.json_response(
                {"ok": False, "error": "sensitive_action"}, status=403
            )
        current_uid.set(uid)
        output = await hermes.dispatch_tool(tool, args)
        favorites_store.record_usage(solaris_db_path, row["owner_uid"], tool, args)
        try:
            result = json.loads(output)
        except (TypeError, ValueError):
            result = {"ok": True, "raw": output}
        return web.json_response({"ok": True, "result": result})

    async def anchors_resolve(request: web.Request) -> web.Response:
        """Resolve auto-anchors (#501) to OKF entity ids (#506).

        Owner-scoped: each `#`/`@` anchor's bare value is matched against
        `entity_aliases`/`entities` (migration 0016) via the same resolver the
        concept aggregator uses. A token without a `#`/`@` prefix is resolved
        whole — this is the `[[X]]` cross-link path (#504), which shares this
        resolver. Returns `{resolved: {token: entity_id}}` for the ones that hit
        a known entity; unresolved tokens are absent so the client keeps phase
        1's `/search` filter chip (anchors) or plain text (`[[ ]]`).
        """
        uid = resolve_uid(request, remote_user_header, default_uid)
        body = await request.json()
        anchors = [str(a) for a in (body.get("anchors") or [])]
        resolved: dict[str, str] = {}
        if anchors and Path(solaris_db_path).exists():
            conn = projection.open_conn(solaris_db_path)
            try:
                for anchor in anchors:
                    value = (
                        anchor[1:].strip()
                        if anchor[:1] in ("#", "@")
                        else anchor.strip()
                    )
                    if not value:
                        continue
                    entity_id = projection.resolve_entity_id(conn, value, uid)
                    if entity_id is not None:
                        resolved[anchor] = entity_id
            except sqlite3.OperationalError:
                pass
            finally:
                conn.close()
        return web.json_response({"ok": True, "resolved": resolved})

    # Auto-linkify alias index (#694): a bounded, server-cached list the client
    # uses to upgrade known references in a rendered reply into concept-page
    # links, code-enforced (the small model rarely emits [[..]]/ANCHORS). Two
    # sources, reusing the existing accessors: HA entities (friendly_name +
    # entity_id → the raw entity_id, which /c/<id> takes directly) and OKF
    # entities (canonical_name + aliases → the entity id). Short aliases (≤2
    # chars) and an over-cap payload are dropped so the single client-side regex
    # stays cheap.
    _ALIAS_CACHE: dict[str, tuple[float, list[dict[str, str]]]] = {}
    _ALIAS_TTL = 300.0
    _ALIAS_CAP = 400

    async def anchors_aliases(request: web.Request) -> web.Response:
        uid = resolve_uid(request, remote_user_header, default_uid)
        cached = _ALIAS_CACHE.get(uid)
        now = time.monotonic()
        if cached is not None and now - cached[0] < _ALIAS_TTL:
            return web.json_response({"ok": True, "aliases": cached[1]})

        seen: set[str] = set()
        pairs: list[tuple[str, str]] = []

        def add(alias: str, target: str) -> None:
            alias = (alias or "").strip()
            key = alias.lower()
            if len(alias) <= 2 or not target or key in seen:
                return
            seen.add(key)
            pairs.append((alias, target))

        if hass_url and hass_token:
            ha_names = await fetch_entity_names(hass_url, hass_token)
            for row in ha_names or []:
                eid = row["entity_id"]
                add(row["name"], eid)
                add(eid, eid)
        if Path(solaris_db_path).exists():
            conn = projection.open_conn(solaris_db_path)
            try:
                for alias, entity_id in projection.linkable_aliases(conn, uid):
                    add(alias, entity_id)
            except sqlite3.OperationalError:
                pass
            finally:
                conn.close()

        # Longest alias first so the client prefers the most specific match;
        # cap the payload so the single-pass regex stays cheap.
        pairs.sort(key=lambda p: len(p[0]), reverse=True)
        aliases = [{"alias": a, "id": i} for a, i in pairs[:_ALIAS_CAP]]
        _ALIAS_CACHE[uid] = (now, aliases)
        return web.json_response({"ok": True, "aliases": aliases})

    async def mentions_tags(request: web.Request) -> web.Response:
        # Autosuggest for `#tag` (#279): the resident's already-used tags,
        # prefix-filtered. Per-resident scope (owner_uid = resolve_uid).
        uid = resolve_uid(request, remote_user_header, default_uid)
        prefix = request.rel_url.query.get("q", "").strip().lstrip("#").lower()
        values = mentions_store.known_tags_for(solaris_db_path, uid, prefix)
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
        used = mentions_store.known_persons_for(solaris_db_path, uid)
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
        items = mentions_store.list_session_mentions(solaris_db_path, session_id, uid)
        return web.json_response({"ok": True, "mentions": items})

    def _resolve_image_hook() -> None:
        # An image-only turn fires the `image-upload` event; the hook that acts
        # on it is resolved from the registry so a rebind in the `/hooks` editor
        # changes which definition handles it (no hardcoded id).
        bound = skills.hooks_for_event(skills_dir, _IMAGE_UPLOAD_EVENT)
        log.info("chat.hook.event", event=_IMAGE_UPLOAD_EVENT, hooks=bound)

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
            _resolve_image_hook()
        session_id = str(body.get("session_id") or "")
        topic_slug = str(body.get("topic") or "").strip()
        ephemeral = bool(body.get("ephemeral"))
        # The shared Zuhause is owned by default_uid but any resident may act in
        # it (#649): owner_uid drives session routing/scope; the real `uid` stays
        # the typed turn's identity (timers/facts) via turn_uid below.
        owner_uid = effective_uid(uid, session_id)
        # Household ("Zuhause") turns are fast-only: never think, never escalate.
        household = is_household_chat(owner_uid, session_id, topic_slug)
        effort = (
            "none"
            if household
            else reasoning.choose_effort(
                text,
                selector=body.get("reasoning"),
                admin=is_admin(request, remote_groups_header, admin_group),
            )
        )
        client = gateway_for(
            request,
            session_id,
            body.get("personality"),
            uid=owner_uid,
            topic_slug=topic_slug,
        )

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
                    owner_uid,
                    topic_slug,
                    text,
                    ephemeral,
                    client,
                )
                owner_uid = effective_uid(uid, session_id)
            elif not ephemeral:
                session_id, compacted = await maybe_compact(
                    owner_uid, session_id, client
                )
            turn_text = topic_turn_text(
                text, uid, session_id, ephemeral=ephemeral, extract_topic=topic_slug
            )
            persist_mentions(uid, session_id, text, ephemeral=ephemeral)
            reply = await client.chat(
                session_id, turn_text, images, effort, turn_uid=uid
            )
        except EngineError:
            return web.json_response(
                {"ok": False, "reason": "engine_unavailable"}, status=502
            )
        attachments.add(session_id, images)
        await persist_turn_trace(owner_uid, session_id, wall_t0, ephemeral=ephemeral)
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
            _resolve_image_hook()
        session_id = str(body.get("session_id") or "")
        topic_slug = str(body.get("topic") or "").strip()
        ephemeral = bool(body.get("ephemeral"))
        # The shared Zuhause is owned by default_uid but any resident may act in
        # it (#649): owner_uid drives session routing/scope; the real `uid` stays
        # the typed turn's identity (timers/facts) via turn_uid below.
        owner_uid = effective_uid(uid, session_id)
        # Household ("Zuhause") turns are fast-only: never think, never escalate.
        household = is_household_chat(owner_uid, session_id, topic_slug)
        effort = (
            "none"
            if household
            else reasoning.choose_effort(
                text,
                selector=body.get("reasoning"),
                admin=is_admin(request, remote_groups_header, admin_group),
            )
        )
        client = gateway_for(
            request,
            session_id,
            body.get("personality"),
            uid=owner_uid,
            topic_slug=topic_slug,
        )

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
        ha_cards: list[dict[str, Any]] = []
        suggestions: list[str] = []
        anchors: list[str] = []
        cancelled = False
        try:
            compacted = False
            if not session_id:
                session_id = await create_turn_session(
                    owner_uid,
                    topic_slug,
                    text,
                    ephemeral,
                    client,
                )
                owner_uid = effective_uid(uid, session_id)
            elif not ephemeral:
                session_id, compacted = await maybe_compact(
                    owner_uid, session_id, client
                )
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
            stream = client.chat_stream(
                session_id,
                turn_text,
                images,
                effort,
                suggest_answers=True,
                turn_uid=uid,
            )
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
                elif name == "ha_cards":
                    ha_cards = data.get("cards") or []
                elif name == "suggestions":
                    suggestions = data.get("suggestions") or []
                elif name == "anchors":
                    anchors = data.get("anchors") or []
                await _send_event(resp, name, data)
            if not cancelled:
                t_end = clock() * 1000.0
                trace = _trace_from_phases(
                    _stream_phases(t_start, t_first, t_think_end, t_end, tool_ms),
                    t_end - t_start,
                )
                await _send_event(resp, "trace", trace)
                record_anchors(uid, session_id, anchors, text, ephemeral=ephemeral)
                await persist_turn_trace(
                    owner_uid,
                    session_id,
                    wall_t0,
                    ephemeral=ephemeral,
                    ha_cards=ha_cards,
                    suggestions=suggestions,
                    anchors=anchors,
                )
        except EngineError:
            await _send_event(resp, "error", {"reason": "engine_unavailable"})
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
    app.router.add_post("/api/ha/call", ha_call)
    app.router.add_get("/api/toolsets", list_toolsets)
    app.router.add_get("/api/mcp", list_mcp)
    app.router.add_post("/api/mcp/{server}/test", test_mcp)
    app.router.add_get("/api/personalities", list_personalities)
    app.router.add_get("/api/skills", list_skills)
    app.router.add_get("/api/skills/{skill_id}", get_skill)
    app.router.add_put("/api/skills/{skill_id}", put_skill)
    app.router.add_get("/api/defs/{kind}", list_defs)
    app.router.add_get("/api/defs/{kind}/{def_id}", get_def)
    app.router.add_put("/api/defs/{kind}/{def_id}", put_def)
    app.router.add_delete("/api/defs/{kind}/{def_id}", delete_def_route)
    app.router.add_get("/api/soul", get_soul)
    app.router.add_put("/api/soul", put_soul)
    app.router.add_get("/api/model", get_model)
    app.router.add_put("/api/model", put_model)
    app.router.add_get("/api/voice", get_voice)
    app.router.add_put("/api/voice", put_voice)
    app.router.add_get("/api/vram", get_vram)
    app.router.add_post("/api/model/pull", pull_model)
    app.router.add_get("/api/sessions", list_sessions)
    app.router.add_post("/api/sessions", create_session)
    app.router.add_get("/api/sessions/{session_id}", get_session)
    app.router.add_delete("/api/sessions/{session_id}", delete_session)
    app.router.add_get("/api/topics", list_topics)
    app.router.add_post("/api/topics", create_topic)
    app.router.add_get("/api/sessions/{session_id}/topics", get_session_topics)
    app.router.add_post("/api/sessions/{session_id}/topics", set_session_topics)
    app.router.add_get("/api/topics/{slug:.+}/items", topic_items)
    app.router.add_get("/api/concept/{id}", concept_view)
    app.router.add_get("/c/{id}", concept_page)
    app.router.add_get("/api/portal/energy", portal_energy)
    app.router.add_get("/api/portal/energy/history", portal_energy_history)
    app.router.add_get("/api/portal/start", portal_start)
    app.router.add_get("/api/portal/start/addable", portal_start_addable)
    app.router.add_post("/api/favorites", favorites_create)
    app.router.add_delete("/api/favorites/{fav_id}", favorites_delete)
    app.router.add_put("/api/favorites/{fav_id}", favorites_reorder)
    app.router.add_post("/api/favorites/{fav_id}/run", favorites_run)
    app.router.add_get("/p/{type}", portal_page)
    app.router.add_post("/api/anchors/resolve", anchors_resolve)
    app.router.add_get("/api/anchors/aliases", anchors_aliases)
    app.router.add_get("/api/mentions/tags", mentions_tags)
    app.router.add_get("/api/mentions/persons", mentions_persons)
    app.router.add_get("/api/sessions/{session_id}/mentions", session_mentions)
    app.router.add_get("/api/sessions/{session_id}/trace", session_trace)
    app.router.add_get("/api/sessions/{session_id}/events", session_events)
    app.router.add_get("/__traces__/{detail_id}", trace_detail)
    app.router.add_post("/api/chat", chat)
    app.router.add_post("/api/chat/stream", chat_stream)
    app.router.add_post("/api/chat/cancel", cancel_chat)
    app.router.add_static("/static/", STATIC_DIR)
    # Ollama-compatible facade under /ollama — HA's `ollama` integration
    # points here so Solaris is the Assist conversation agent; the gatekeeper
    # speaks the same surface for wyoming-satellite hardware.
    if hasattr(hermes, "respond"):
        facade_clients = {"solaris": hermes, "solaris-deep": deep_gw}
        # The guest profile (#353) is reachable as its own model but not yet
        # auto-triggered — speaker-ID routing into it is #351 (blocked).
        if hermes_guest is not None:
            facade_clients["solaris-guest"] = hermes_guest
        add_facade_routes(
            app,
            clients=facade_clients,
            api_key=api_key,
            default_uid=default_uid,
            solaris_db_path=solaris_db_path,
        )
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

    The browser needs a token delta, a tool start/stop hint, an end marker,
    and (for the live activity bubble, #347) a per-LLM-pass `step`. Anything
    else collapses to a no-op `keepalive`.
    """
    etype = str(event.get("type") or "")
    data = event.get("data")
    payload = data if isinstance(data, dict) else {}
    if etype == "assistant.delta":
        text = payload.get("delta") or payload.get("text") or payload.get("content")
        if not text and isinstance(data, str):
            text = data
        return "delta", {"text": str(text or "")}
    if etype == "llm.step":
        return "step", {
            "label": str(payload.get("model") or "llm"),
            "wall_s": payload.get("wall_s"),
        }
    if etype in ("tool.started", "tool.completed"):
        name = payload.get("tool") or payload.get("name") or ""
        phase = "started" if etype == "tool.started" else "completed"
        out = {"name": str(name), "phase": phase}
        if etype == "tool.completed" and payload.get("wall_s") is not None:
            out["wall_s"] = payload["wall_s"]
        return "tool", out
    if etype == "ha_cards":
        return "ha_cards", {"cards": payload.get("cards") or []}
    if etype == "quick_replies":
        return "quick_replies", {"options": payload.get("options") or []}
    if etype == "suggestions":
        return "suggestions", {"suggestions": payload.get("suggestions") or []}
    if etype == "anchors":
        return "anchors", {"anchors": payload.get("anchors") or []}
    if etype == "run.completed":
        return "completed", {
            "reasoning": _reasoning_from_completed(payload),
            "answer": _answer_from_messages(payload.get("messages")),
        }
    return "keepalive", {}


def _answer_from_messages(messages: Any) -> str:
    """Last assistant `content` from a `run.completed` messages array, else "".

    Tool-invocation turns surface the model's final answer here rather than in
    streaming deltas, so both chat paths fall back to it (#258). The reasoning
    lives in a separate field and is skipped.
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
    hermes: EngineClient,
    hermes_admin: EngineClient | None = None,
    hermes_deep: EngineClient | None = None,
    hermes_guest: EngineClient | None = None,
    remote_user_header: str,
    default_uid: str,
    remote_groups_header: str = "Remote-Groups",
    admin_group: str = "admins",
    skills_dir: str = "/data/skills",
    soul_path: str = "/data/SOUL.md",
    logout_url: str = "",
    context_window: ContextWindow,
    compaction_threshold: float = compaction.DEFAULT_THRESHOLD,
    attachments_dir: str = "/data/attachments",
    frame_ancestors: str = "'self'",
    fast_model: str = "",
    thorough_model: str = "",
    tts_voices: str = "martin",
    solaris_db_path: str = "/var/lib/solaris/solaris.db",
    notes_dir: str = "/opt/data/notes",
    ollama_url: str = "http://127.0.0.1:11434",
    trace_recorder: Any = None,
    api_key: str = "",
    bus: Any = None,
    sb_mcp_url: str = "",
    sb_mcp_token_path: str = "",
    hass_url: str = "",
    hass_token: str = "",
) -> None:
    if isinstance(context_window, int):
        context_window = ContextWindow.static(context_window)
    app = build_app(
        hermes=hermes,
        hermes_admin=hermes_admin,
        hermes_deep=hermes_deep,
        hermes_guest=hermes_guest,
        remote_user_header=remote_user_header,
        default_uid=default_uid,
        remote_groups_header=remote_groups_header,
        admin_group=admin_group,
        skills_dir=skills_dir,
        soul_path=soul_path,
        logout_url=logout_url,
        context_window=context_window,
        compaction_threshold=compaction_threshold,
        attachments_dir=attachments_dir,
        frame_ancestors=frame_ancestors,
        fast_model=fast_model,
        thorough_model=thorough_model,
        tts_voices=tts_voices,
        solaris_db_path=solaris_db_path,
        notes_dir=notes_dir,
        ollama_url=ollama_url,
        trace_recorder=trace_recorder,
        api_key=api_key,
        bus=bus,
        sb_mcp_url=sb_mcp_url,
        sb_mcp_token_path=sb_mcp_token_path,
        hass_url=hass_url,
        hass_token=hass_token,
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

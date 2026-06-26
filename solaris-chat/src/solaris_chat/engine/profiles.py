"""Profile assembly — three constructor calls replace three Hermes gateways.

household — fast model, never thinks, full household toolbox + the injected
            entity registry (the voice/chat hot path, ≤3k-token prompt).
deep      — thorough model, thinks by default, same household toolbox + the
            registry (the "Solaris Gründlich" mode and the night crons).
admin     — thorough model + the admin soul + the operator skill pack as
            prompt, with the `servicebay_admin` MCP toolbox (read+lifecycle+
            mutate scopes — Phase 3).
guest     — fast model, restricted toolbox (HA control/state + web Q&A, no
            notes/timers/admin), and ephemeral: a guest turn writes nothing to
            the store, so nothing about a guest survives the conversation (#353).

They share one store, one Ollama client, one trace recorder — a turn's
profile decides prompt + model + tools, nothing else.
"""

from __future__ import annotations

from pathlib import Path

from solaris_chat import settings_store
from solaris_chat.engine import client as engine_client
from solaris_chat.engine.bus import SessionBus
from solaris_chat.engine.client import EngineClient, EngineProfile
from solaris_chat.engine.ingest.jellyfin import RestJellyfinMusicClient
from solaris_chat.engine.ollama import OllamaChat
from solaris_chat.engine.registry import EntityRegistry
from solaris_chat.engine.tools import Tool, Toolbox
from solaris_chat.engine.tools.choices import build_choice_tools
from solaris_chat.engine.tools.ha import build_ha_tools
from solaris_chat.engine.tools.mcp_tools import CombinedToolbox, McpToolbox
from solaris_chat.engine.tools.media import build_media_tools
from solaris_chat.engine.tools.music_query import build_music_query_tools
from solaris_chat.engine.tools.notes import build_notes_tools
from solaris_chat.engine.tools.onboarding_approval import (
    build_onboarding_approval_tools,
)
from solaris_chat.engine.tools.radio import build_radio_tools
from solaris_chat.engine.tools.register import build_register_tools
from solaris_chat.engine.tools.research import build_research_tools
from solaris_chat.engine.tools.skill_promotion import build_skill_promotion_tools
from solaris_chat.engine.tools.timers import build_timer_tools
from solaris_chat.engine.tools.web import build_web_tools
from solaris_chat.engine.trace import TraceRecorder


def _current_uid() -> str:
    return engine_client.current_uid.get()


def _current_room() -> str:
    return engine_client.current_room.get()


def _skills_prompt(skills_dir: str) -> str:
    """Concatenated SKILL.md bodies (frontmatter stripped) — the prompt-
    assembly form of a skill pack."""
    if not skills_dir:
        return ""
    parts: list[str] = []
    for path in sorted(Path(skills_dir).glob("*/SKILL.md")):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if text.startswith("---"):
            end = text.find("---", 3)
            if end != -1:
                text = text[end + 3 :]
        if text.strip():
            parts.append(text.strip())
    return "\n\n".join(parts)


def build_engine_clients(
    *,
    db_path: str,
    ollama_url: str,
    fast_model: str,
    thorough_model: str,
    soul_path: str,
    admin_soul_path: str = "",
    admin_skills_dir: str = "",
    skills_dir: str = "",
    sb_mcp_url: str = "",
    sb_mcp_token_path: str = "",
    hass_url: str = "",
    hass_token: str = "",
    tavily_api_key: str = "",
    notes_dir: str = "",
    gatekeeper_url: str = "",
    gatekeeper_token: str = "",
    context_window: int | None = None,
    default_uid: str = "household",
    jellyfin_url: str = "",
    jellyfin_cast_url: str = "",
    jellyfin_username: str = "",
    jellyfin_password: str = "",
) -> tuple[
    EngineClient, EngineClient, EngineClient, EngineClient, TraceRecorder, SessionBus
]:
    """Returns (household, deep, admin, guest) clients + the recorder + bus."""
    ollama = OllamaChat(ollama_url)
    recorder = TraceRecorder()
    bus = SessionBus()
    registry = EntityRegistry(hass_url, hass_token)

    ha_tools: list[Tool] = (
        build_ha_tools(hass_url, hass_token) if hass_url and hass_token else []
    )
    web_tools = build_web_tools(tavily_api_key)
    # Research-synthesis (#574): one tool does gather+trust-rank+cite so the
    # small model only phrases. Rides with the web fan-out, so it's gated on web
    # availability exactly as the web tools are; it pulls in the notes vault too.
    research_tools = build_research_tools(
        notes_dir=notes_dir,
        uid_getter=_current_uid,
        tavily_api_key=tavily_api_key,
    )

    # Quick-reply chips (#555): offered on any profile that holds a conversation,
    # so household, deep and guest all get the offer_choices tool.
    choice_tools = build_choice_tools()

    household_tools: list[Tool] = list(ha_tools)
    household_tools += build_timer_tools(db_path, _current_uid)
    household_tools += web_tools
    household_tools += research_tools
    household_tools += choice_tools
    if hass_url and hass_token:
        household_tools += build_media_tools(hass_url, hass_token)
        # play_radio (#u94): casts a resident's favorite station via the same
        # scoped HA play_media path; the favorite is a per-user note, so it needs
        # the vault as well. Household + deep share this list (not guest).
        if notes_dir:
            household_tools += build_radio_tools(
                notes_dir,
                hass_url,
                hass_token,
                _current_uid,
                room_getter=_current_room,
                room_resolver=registry.media_player_for_room,
            )
    if notes_dir:
        household_tools += build_notes_tools(notes_dir, _current_uid)
    # Structured music-library queries (#588): household + deep share this list,
    # so both get music_query; guest (its own list below) is withheld. A live
    # Jellyfin client (built once, the same read-only creds the ingest uses) is
    # passed in so on-demand lyrics (#593) can fetch /Audio/{id}/Lyrics at query
    # time; when Jellyfin is unconfigured the other ops still register and
    # song_lyrics degrades gracefully ("keine Lyrics verfügbar").
    if db_path:
        lyrics_client = (
            RestJellyfinMusicClient(
                jellyfin_url,
                jellyfin_username,
                jellyfin_password,
                cast_base_url=jellyfin_cast_url or None,
            )
            if jellyfin_url
            else None
        )
        # play_music (#604) casts a library track via the same scoped HA
        # play_media path; it registers only when a Jellyfin client + HA creds
        # are present, so on household+deep (not guest) and not when unconfigured.
        household_tools += build_music_query_tools(
            db_path,
            _current_uid,
            lyrics_client,
            hass_url=hass_url,
            hass_token=hass_token,
            room_getter=_current_room,
            room_resolver=registry.media_player_for_room,
            notes_dir=notes_dir,
        )
    # First-run/owner self-enrolment (#396): with zero enrolments an unknown
    # speaker resolves to `household`, not `guest`, so the guest-onboarding path
    # can never bootstrap the first voice profile. Give the household profile the
    # same enrol tools so a spoken "Setup starten" can file a (still
    # admin-approved, #355) registration. It only ever files a pending request —
    # no account, no resident access — so it's the same one durable, gated write
    # the guest path makes.
    if gatekeeper_url:
        household_tools += build_register_tools(
            db_path, gatekeeper_url, gatekeeper_token
        )

    # A guest may ask questions (web) and control devices/read state (HA), but
    # may NOT write anything durable — no notes/fact_store, no timers, no admin
    # MCP. The denial is the absence of those tool modules here (#353).
    # ha_run_scene_script fires whole routines/automations; that's beyond a
    # guest's "simple home control" remit, so it's withheld here (#370).
    guest_tools: list[Tool] = (
        [t for t in ha_tools if t.name != "ha_run_scene_script"]
        + list(web_tools)
        + list(research_tools)
        + choice_tools
    )
    # The registration flow runs under the guest profile (an unknown speaker is
    # a guest turn, #353) but only the onboarding skill ever invokes it: enrol
    # the voice + file a pending request (#376). It's the one durable write a
    # guest turn can make, and only into the approval queue — never the store.
    if gatekeeper_url:
        guest_tools += build_register_tools(db_path, gatekeeper_url, gatekeeper_token)

    def make(profile: EngineProfile) -> EngineClient:
        return EngineClient(
            profile,
            db_path=db_path,
            ollama=ollama,
            recorder=recorder,
            context_window=context_window,
            bus=bus,
        )

    household = make(
        EngineProfile(
            name="household",
            model=fast_model or "gemma4:e2b",
            # Admin-selectable from the panel (#366): the persisted override wins
            # per turn, falling back to the FAST_MODEL default when unset — so the
            # fast-only default holds for installs that never touch the picker.
            model_resolver=lambda: settings_store.get_household_model(db_path),
            soul_path=soul_path,
            registry=registry,
            think_default=False,
            temperature=0.2,
            toolbox=Toolbox(household_tools),
            default_uid=default_uid,
        )
    )
    deep = make(
        EngineProfile(
            name="solaris-deep",
            model=thorough_model or "gemma4:12b",
            soul_path=soul_path,
            registry=registry,
            think_default=True,
            toolbox=Toolbox(household_tools),
            default_uid=default_uid,
        )
    )
    # Admin gets the remote SB-MCP operator tools plus the local onboarding-
    # approval tools (#355): filing/polling a resident request rides SB's MCP,
    # but flipping the pending row + confirming the voice binding is a local
    # side-effect, so it lives in code, not in whatever the model remembers.
    admin_toolbox: Toolbox
    if sb_mcp_url:
        local_admin_tools = build_onboarding_approval_tools(
            db_path,
            sb_mcp_url,
            sb_mcp_token_path,
            gatekeeper_url,
            gatekeeper_token,
        )
        # Dynamic-skill promotion (#427) rides the same generic SB approval API:
        # the admin files/polls the request, and on approval the engine moves the
        # draft into the active pack itself — no service restart.
        if skills_dir:
            local_admin_tools += build_skill_promotion_tools(
                skills_dir, sb_mcp_url, sb_mcp_token_path
            )
        admin_toolbox = CombinedToolbox(
            McpToolbox(sb_mcp_url, sb_mcp_token_path),
            Toolbox(local_admin_tools),
        )
    else:
        admin_toolbox = Toolbox([])
    admin = make(
        EngineProfile(
            name="admin",
            model=thorough_model or "gemma4:12b",
            soul_path=admin_soul_path or soul_path,
            extra_prompt=_skills_prompt(admin_skills_dir),
            think_default=True,
            toolbox=admin_toolbox,
            default_uid=default_uid,
        )
    )
    guest = make(
        EngineProfile(
            name="solaris-guest",
            model=fast_model or "gemma4:e2b",
            soul_path=soul_path,
            registry=registry,
            think_default=False,
            temperature=0.2,
            toolbox=Toolbox(guest_tools),
            ephemeral=True,
            default_uid=default_uid,
        )
    )
    return household, deep, admin, guest, recorder, bus

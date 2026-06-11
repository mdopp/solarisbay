"""Profile assembly — three constructor calls replace three Hermes gateways.

household — fast model, never thinks, full household toolbox + the injected
            entity registry (the voice/chat hot path, ≤3k-token prompt).
deep      — thorough model, thinks by default, same household toolbox + the
            registry (the "Sol Gründlich" mode and, later, the night crons).
admin     — thorough model + the admin soul; ServiceBay MCP tools arrive in
            Phase 3, until then it is a tool-less operator chat.

All three share one store, one Ollama client, one trace recorder — a turn's
profile decides prompt + model + tools, nothing else.
"""

from __future__ import annotations

from solilos_chat.engine import client as engine_client
from solilos_chat.engine.client import EngineClient, EngineProfile
from solilos_chat.engine.ollama import OllamaChat
from solilos_chat.engine.registry import EntityRegistry
from solilos_chat.engine.tools import Tool, Toolbox
from solilos_chat.engine.tools.ha import build_ha_tools
from solilos_chat.engine.tools.notes import build_notes_tools
from solilos_chat.engine.tools.timers import build_timer_tools
from solilos_chat.engine.tools.web import build_web_tools
from solilos_chat.engine.trace import TraceRecorder


def _current_uid() -> str:
    return engine_client.current_uid.get()


def build_engine_clients(
    *,
    db_path: str,
    ollama_url: str,
    fast_model: str,
    thorough_model: str,
    soul_path: str,
    admin_soul_path: str = "",
    hass_url: str = "",
    hass_token: str = "",
    tavily_api_key: str = "",
    notes_dir: str = "",
    context_window: int | None = None,
) -> tuple[EngineClient, EngineClient, EngineClient, TraceRecorder]:
    """Returns (household, deep, admin) clients + their shared recorder."""
    ollama = OllamaChat(ollama_url)
    recorder = TraceRecorder()
    registry = EntityRegistry(hass_url, hass_token)

    household_tools: list[Tool] = []
    if hass_url and hass_token:
        household_tools += build_ha_tools(hass_url, hass_token)
    household_tools += build_timer_tools(db_path, _current_uid)
    household_tools += build_web_tools(tavily_api_key)
    if notes_dir:
        household_tools += build_notes_tools(notes_dir, _current_uid)

    def make(profile: EngineProfile) -> EngineClient:
        return EngineClient(
            profile,
            db_path=db_path,
            ollama=ollama,
            recorder=recorder,
            context_window=context_window,
        )

    household = make(
        EngineProfile(
            name="household",
            model=fast_model or "gemma4:e2b",
            soul_path=soul_path,
            registry=registry,
            think_default=False,
            toolbox=Toolbox(household_tools),
        )
    )
    deep = make(
        EngineProfile(
            name="sol-deep",
            model=thorough_model or "gemma4:12b",
            soul_path=soul_path,
            registry=registry,
            think_default=True,
            toolbox=Toolbox(household_tools),
        )
    )
    admin = make(
        EngineProfile(
            name="admin",
            model=thorough_model or "gemma4:12b",
            soul_path=admin_soul_path or soul_path,
            think_default=True,
            toolbox=Toolbox([]),
        )
    )
    return household, deep, admin, recorder

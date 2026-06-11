"""Entrypoint: run the chat server on the Sol Engine."""

from __future__ import annotations

import asyncio

from solilos_chat.config import settings
from solilos_chat.context import build_context_window
from solilos_chat.engine.profiles import build_engine_clients
from solilos_chat.engine.scheduler import TimerScheduler
from solilos_chat.logging import log
from solilos_chat.server import serve


async def _run() -> None:
    context_window = await build_context_window(
        settings.ollama_url, settings.context_window_override
    )
    household, deep, admin, recorder = build_engine_clients(
        db_path=settings.solilos_db_path,
        ollama_url=settings.ollama_url,
        fast_model=settings.fast_model,
        thorough_model=settings.thorough_model,
        soul_path=settings.soul_path,
        admin_soul_path=settings.admin_soul_path,
        hass_url=settings.hass_url,
        hass_token=settings.hass_token,
        tavily_api_key=settings.tavily_api_key,
        notes_dir=settings.notes_dir,
        context_window=context_window.value,
    )
    scheduler = TimerScheduler(
        settings.solilos_db_path, settings.hass_url, settings.hass_token
    )
    scheduler.start()
    await serve(
        settings.host,
        settings.port,
        hermes=household,
        hermes_admin=admin,
        hermes_deep=deep,
        remote_user_header=settings.remote_user_header,
        default_uid=settings.default_uid,
        remote_groups_header=settings.remote_groups_header,
        admin_group=settings.admin_group,
        skills_dir=settings.skills_dir,
        soul_path=settings.soul_path,
        config_agent_url=settings.config_agent_url,
        agent_token=settings.hermes_token,
        logout_url=settings.logout_url,
        context_window=context_window,
        compaction_threshold=settings.compaction_threshold,
        attachments_dir=settings.attachments_dir,
        frame_ancestors=settings.frame_ancestors,
        fast_model=settings.fast_model,
        thorough_model=settings.thorough_model,
        solilos_db_path=settings.solilos_db_path,
        notes_dir=settings.notes_dir,
        trace_proxy_url=settings.trace_proxy_url,
        trace_recorder=recorder,
    )


def main() -> None:
    log.info(
        "chat.boot",
        host=settings.host,
        port=settings.port,
        ollama=settings.ollama_url,
        engine="sol",
    )
    asyncio.run(_run())


if __name__ == "__main__":
    main()

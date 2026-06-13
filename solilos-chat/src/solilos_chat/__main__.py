"""Entrypoint: run the chat server on the Sol Engine."""

from __future__ import annotations

import asyncio

from solilos_chat.config import settings
from solilos_chat.context import build_context_window
from solilos_chat.engine.crons import CronRunner
from solilos_chat.engine.profiles import build_engine_clients
from solilos_chat.engine.scheduler import TimerScheduler
from solilos_chat.logging import log
from solilos_chat.server import serve


async def _run() -> None:
    context_window = await build_context_window(
        settings.ollama_url, settings.context_window_override
    )
    household, deep, admin, guest, recorder, bus = build_engine_clients(
        db_path=settings.solilos_db_path,
        ollama_url=settings.ollama_url,
        fast_model=settings.fast_model,
        thorough_model=settings.thorough_model,
        soul_path=settings.soul_path,
        admin_soul_path=settings.admin_soul_path,
        admin_skills_dir=settings.admin_skills_dir,
        sb_mcp_url=settings.sb_mcp_url,
        sb_mcp_token_path=settings.sb_mcp_token_path,
        hass_url=settings.hass_url,
        hass_token=settings.hass_token,
        tavily_api_key=settings.tavily_api_key,
        notes_dir=settings.notes_dir,
        gatekeeper_url=settings.gatekeeper_url,
        gatekeeper_token=settings.gatekeeper_token,
        context_window=context_window.value,
        default_uid=settings.default_uid,
    )
    scheduler = TimerScheduler(
        settings.solilos_db_path,
        settings.hass_url,
        settings.hass_token,
        settings.alarm_sound_media_id,
        settings.alarm_sound_path,
    )
    scheduler.start()
    crons = CronRunner(
        db_path=settings.solilos_db_path,
        deep=deep,
        skills_dir=settings.skills_dir,
        context_window=context_window.value,
    )
    crons.start()
    await serve(
        settings.host,
        settings.port,
        hermes=household,
        hermes_admin=admin,
        hermes_deep=deep,
        hermes_guest=guest,
        remote_user_header=settings.remote_user_header,
        default_uid=settings.default_uid,
        remote_groups_header=settings.remote_groups_header,
        admin_group=settings.admin_group,
        skills_dir=settings.skills_dir,
        soul_path=settings.soul_path,
        logout_url=settings.logout_url,
        context_window=context_window,
        compaction_threshold=settings.compaction_threshold,
        attachments_dir=settings.attachments_dir,
        frame_ancestors=settings.frame_ancestors,
        fast_model=settings.fast_model,
        thorough_model=settings.thorough_model,
        tts_voices=settings.tts_voices,
        solilos_db_path=settings.solilos_db_path,
        notes_dir=settings.notes_dir,
        ollama_url=settings.ollama_url,
        trace_recorder=recorder,
        api_key=settings.api_key,
        bus=bus,
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

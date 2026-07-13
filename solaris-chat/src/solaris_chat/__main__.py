"""Entrypoint: run the chat server on the Solaris Engine."""

from __future__ import annotations

import asyncio
import threading

from solaris_chat.config import settings
from solaris_chat.context import build_context_window
from solaris_chat.engine.crons import CronRunner
from solaris_chat.engine.ha_watch import HaStateWatcher
from solaris_chat.engine.ingest import run_ingest
from solaris_chat.engine.notify import EventBus, Notifier
from solaris_chat.engine.profiles import build_engine_clients
from solaris_chat.engine.scheduler import TimerScheduler
from solaris_chat.logging import log
from solaris_chat.server import serve


async def _run() -> None:
    context_window = await build_context_window(
        settings.ollama_url, settings.context_window_override
    )
    household, deep, admin, guest, librarian, recorder, bus = build_engine_clients(
        db_path=settings.solaris_db_path,
        ollama_url=settings.ollama_url,
        fast_model=settings.fast_model,
        thorough_model=settings.thorough_model,
        soul_path=settings.soul_path,
        admin_soul_path=settings.admin_soul_path,
        admin_skills_dir=settings.admin_skills_dir,
        skills_dir=settings.skills_dir,
        sb_mcp_url=settings.sb_mcp_url,
        sb_mcp_token_path=settings.sb_mcp_token_path,
        sb_api_url=settings.sb_api_url,
        hass_url=settings.hass_url,
        hass_token=settings.hass_token,
        tavily_api_key=settings.tavily_api_key,
        notes_dir=settings.notes_dir,
        gatekeeper_url=settings.gatekeeper_url,
        gatekeeper_token=settings.gatekeeper_token,
        context_window=context_window.value,
        default_uid=settings.default_uid,
        jellyfin_url=settings.jellyfin_url,
        jellyfin_cast_url=settings.jellyfin_cast_url,
        jellyfin_username=settings.jellyfin_username,
        jellyfin_password=settings.jellyfin_password,
    )
    notifier = Notifier(
        settings.solaris_db_path,
        settings.vapid_public_key,
        settings.vapid_private_key,
        settings.vapid_subject,
    )
    scheduler = TimerScheduler(
        settings.solaris_db_path,
        settings.hass_url,
        settings.hass_token,
        settings.alarm_sound_media_id,
        settings.alarm_sound_path,
        notifier=notifier,
    )
    scheduler.start()
    # Live status propagation (#714): the event bus fans HA state changes to
    # every open /p/start client via SSE; the HA-WS watcher feeds it, bounded to
    # the residents' pinned entities, and pushes only noteworthy transitions when
    # no client is watching that uid.
    event_bus = EventBus()
    ha_watcher = HaStateWatcher(
        settings.hass_url,
        settings.hass_token,
        event_bus,
        settings.solaris_db_path,
        notifier=notifier,
    )
    ha_watcher.start()
    crons = CronRunner(
        db_path=settings.solaris_db_path,
        deep=deep,
        skills_dir=settings.skills_dir,
        context_window=context_window.value,
        ingest_settings=settings,
        librarian=librarian,
    )
    crons.start()

    # Populate the OKF store on boot (#517). Run in a dedicated worker thread
    # with its own event loop, NOT as a task on the chat server's loop: the
    # ingest does synchronous sqlite writes + per-asset embedding work, and on
    # the main loop that grinds through the whole Immich/Jellyfin library while
    # blocking every request — /health times out for the entire run and the
    # chat becomes unreachable (#586). Daemon so it never delays shutdown;
    # run_ingest never raises, but guard the thread regardless.
    def _bg_ingest() -> None:
        try:
            asyncio.run(run_ingest(settings))
        except Exception as e:  # noqa: BLE001 — ingest must never crash the box.
            log.error("engine.ingest.thread_failed", error=str(e))

    threading.Thread(target=_bg_ingest, name="okf-ingest", daemon=True).start()
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
        solaris_db_path=settings.solaris_db_path,
        notes_dir=settings.notes_dir,
        ollama_url=settings.ollama_url,
        trace_recorder=recorder,
        api_key=settings.api_key,
        bus=bus,
        event_bus=event_bus,
        notifier=notifier,
        sb_mcp_url=settings.sb_mcp_url,
        sb_mcp_token_path=settings.sb_mcp_token_path,
        hass_url=settings.hass_url,
        hass_token=settings.hass_token,
        crons=crons,
        vapid_public_key=settings.vapid_public_key,
        android_package=settings.android_package,
        android_cert_fingerprints=settings.android_cert_fingerprints,
        ha_watcher=ha_watcher,
    )


def main() -> None:
    log.info(
        "chat.boot",
        host=settings.host,
        port=settings.port,
        ollama=settings.ollama_url,
        engine="solaris",
    )
    asyncio.run(_run())


if __name__ == "__main__":
    main()

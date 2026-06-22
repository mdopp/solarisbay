"""On-boot OKF ingest trigger (#517).

The Phase-1 ingest adapters (Obsidian #448, Immich #206, CalDAV/CardDAV #207)
are merged but nothing ever *runs* them, so the OKF store stays empty on the
box. This runner is that missing trigger: it runs each adapter once, in the
background, after the engine boots — populating `notes/okf/`, the `.db`
projection and the embedding queue.

Degradation is deliberate (§ guardrails): the Obsidian adapter reads the local
vault and always runs; Immich and CalDAV/CardDAV run only when their source
config is present, and any adapter failure is logged and skipped rather than
crashing the trigger. No source credential is read from anything but the
process env (`config.Settings`).
"""

from __future__ import annotations

from solaris_chat.config import Settings
from solaris_chat.logging import log

from ..knowledge import PendingEmbeddingQueue
from ..knowledge.writer import OkfWriter
from .caldav import DavIngest
from .dav_client import HttpDavClient
from .immich import ImmichIngest
from .immich_client import RestImmichClient
from .obsidian import ObsidianIngest
from .obsidian_reader import VaultObsidianReader


async def run_ingest(settings: Settings) -> None:
    """Run every configured OKF ingest adapter once. Never raises."""
    writer = OkfWriter(
        db_path=settings.solaris_db_path,
        notes_dir=settings.notes_dir,
        embedding_queue=PendingEmbeddingQueue(settings.solaris_db_path),
    )
    uid = settings.default_uid

    _run_obsidian(settings, writer, uid)
    await _run_immich(settings, writer, uid)
    await _run_caldav(settings, writer, uid)


def _run_obsidian(settings: Settings, writer: OkfWriter, uid: str) -> None:
    """The vault adapter needs no external creds — always runs."""
    try:
        reader = VaultObsidianReader(settings.notes_dir)
        ingest = ObsidianIngest(
            reader, writer, db_path=settings.solaris_db_path, ingesting_uid=uid
        )
        stats = ingest.run()
        log.info(
            "engine.ingest.obsidian",
            notes=stats.notes,
            written=stats.written,
            skipped=stats.skipped,
        )
    except Exception as e:  # noqa: BLE001 — one adapter failing must not crash boot.
        log.error("engine.ingest.obsidian_failed", error=str(e))


async def _run_immich(settings: Settings, writer: OkfWriter, uid: str) -> None:
    if not (settings.immich_base_url and settings.immich_api_key):
        log.info("engine.ingest.immich_skipped", reason="unconfigured")
        return
    try:
        client = RestImmichClient(settings.immich_base_url, settings.immich_api_key)
        stats = await ImmichIngest(client, writer, ingesting_uid=uid).run()
        log.info(
            "engine.ingest.immich",
            assets=stats.assets,
            events=stats.events_written,
            people=stats.people_written,
            places=stats.places_written,
        )
    except Exception as e:  # noqa: BLE001 — degrade gracefully on any source error.
        log.error("engine.ingest.immich_failed", error=str(e))


async def _run_caldav(settings: Settings, writer: OkfWriter, uid: str) -> None:
    if not (settings.caldav_url or settings.carddav_url):
        log.info("engine.ingest.caldav_skipped", reason="unconfigured")
        return
    try:
        client = HttpDavClient(
            caldav_url=settings.caldav_url,
            caldav_username=settings.caldav_username,
            caldav_password=settings.caldav_password,
            carddav_url=settings.carddav_url,
            carddav_username=settings.carddav_username,
            carddav_password=settings.carddav_password,
        )
        stats = await DavIngest(client, writer, ingesting_uid=uid).run()
        log.info(
            "engine.ingest.caldav",
            contacts=stats.contacts,
            people=stats.people_written,
            events=stats.events,
            events_written=stats.events_written,
        )
    except Exception as e:  # noqa: BLE001 — degrade gracefully on any source error.
        log.error("engine.ingest.caldav_failed", error=str(e))

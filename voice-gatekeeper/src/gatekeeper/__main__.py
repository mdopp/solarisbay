"""Gatekeeper entry point — start the Wyoming server + push-HTTP server.

Both run as concurrent tasks under one asyncio loop. If either crashes,
the process exits so the pod restarts and recovers a consistent state.
"""

from __future__ import annotations

import asyncio

from gatekeeper.logging import log
from wyoming.info import AsrModel, AsrProgram, Attribution, Info, TtsProgram
from wyoming.server import AsyncServer

from . import __version__ as GATEKEEPER_VERSION
from .config import settings
from .handler import GatekeeperHandler
from .push import serve as serve_push


def _info() -> Info:
    """Self-describe so satellites can introspect what we offer.

    Phase 0 advertises the gatekeeper as a combined ASR+TTS pipeline server.
    The underlying models are configured via env vars (Whisper + Piper);
    satellite clients see one logical endpoint here.

    `version` is required by wyoming>=1.6 on AsrProgram, AsrModel, and
    TtsProgram. Before that release it was Optional and the call sites
    here omitted it; the image now pins wyoming>=1.9 (see pyproject.toml)
    so the omission would be a TypeError on every Wyoming connection —
    see #1024 for the live failure mode it caused.
    """
    return Info(
        asr=[
            AsrProgram(
                name="oscar-gatekeeper-asr",
                description="OSCAR gatekeeper — ASR via internal Whisper",
                attribution=Attribution(
                    name="OSCAR", url="https://github.com/mdopp/servicebay"
                ),
                installed=True,
                version=GATEKEEPER_VERSION,
                models=[
                    AsrModel(
                        name="oscar-gatekeeper",
                        description="Gatekeeper pipeline (Whisper -> HERMES -> Piper)",
                        attribution=Attribution(
                            name="OSCAR", url="https://github.com/mdopp/servicebay"
                        ),
                        installed=True,
                        version=GATEKEEPER_VERSION,
                        languages=["de", "en"],
                    )
                ],
            )
        ],
        tts=[
            TtsProgram(
                name="oscar-gatekeeper-tts",
                description="OSCAR gatekeeper — TTS via internal Piper",
                attribution=Attribution(
                    name="OSCAR", url="https://github.com/mdopp/servicebay"
                ),
                installed=True,
                version=GATEKEEPER_VERSION,
                voices=[],
            )
        ],
    )


async def _serve_wyoming() -> None:
    server = AsyncServer.from_uri(settings.gatekeeper_uri)
    log.info("gatekeeper.boot", uri=settings.gatekeeper_uri)
    await server.run(lambda r, w: GatekeeperHandler(r, w, _info()))


async def _serve() -> None:
    wyoming = asyncio.create_task(_serve_wyoming(), name="wyoming")
    push = asyncio.create_task(
        serve_push(
            settings.push_host,
            settings.push_port,
            piper_uri=settings.piper_uri,
            devices=settings.voice_pe_devices,
            push_token=settings.push_token,
            db_path=settings.oscar_db_path if settings.speaker_id_enabled else None,
        ),
        name="push",
    )
    done, pending = await asyncio.wait(
        {wyoming, push}, return_when=asyncio.FIRST_COMPLETED
    )
    for task in pending:
        task.cancel()
    for task in done:
        if task.exception():
            log.error(
                "gatekeeper.task.crashed",
                task=task.get_name(),
                error=str(task.exception()),
            )
            raise task.exception()  # propagate so the pod restarts


def main() -> None:
    asyncio.run(_serve())


if __name__ == "__main__":
    main()

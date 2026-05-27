"""Wyoming event handler for the gatekeeper.

One handler instance per inbound connection. The Phase-0 contract:

  Client → AudioStart, AudioChunk*, AudioStop
  Gatekeeper:
    1. Stream the buffered audio to whisper, await Transcript
    2. POST transcript to HERMES with (uid, endpoint, trace_id)
    3. Send response text to piper, stream the resulting AudioChunks back
       to the original client

The connection closes after one pipeline turn (Phase 0 is half-duplex per
turn, like HA's voice pipeline). Multi-turn / streaming is a Phase 4 topic.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from gatekeeper.logging import log
from wyoming.asr import Transcribe, Transcript
from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.client import AsyncClient
from wyoming.event import Event
from wyoming.server import AsyncEventHandler

from .config import settings
from .embeddings_store import list_embeddings, touch_last_seen
from .hermes import HermesClient
from .speaker import get_extractor, resolve_speaker
from .tts import synthesize_to_writer


class GatekeeperHandler(AsyncEventHandler):
    """One connection = one pipeline turn."""

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.trace_id = str(uuid.uuid4())
        self._audio_start: AudioStart | None = None
        self._audio_buffer: list[AudioChunk] = []
        self._hermes = HermesClient(settings.hermes_url, settings.hermes_token)
        log.info("gatekeeper.session.open", trace_id=self.trace_id)

    async def handle_event(self, event: Event) -> bool:
        if AudioStart.is_type(event.type):
            self._audio_start = AudioStart.from_event(event)
            self._audio_buffer = []
            log.info(
                "gatekeeper.audio.start",
                trace_id=self.trace_id,
                rate=self._audio_start.rate,
                width=self._audio_start.width,
                channels=self._audio_start.channels,
            )
            return True

        if AudioChunk.is_type(event.type):
            self._audio_buffer.append(AudioChunk.from_event(event))
            return True

        if AudioStop.is_type(event.type):
            log.info(
                "gatekeeper.audio.stop",
                trace_id=self.trace_id,
                chunks=len(self._audio_buffer),
            )
            await self._process_pipeline()
            return False

        # Unknown event types are dropped silently; debug-mode shows them.
        log.debug("gatekeeper.event.unhandled", trace_id=self.trace_id, type=event.type)
        return True

    async def _process_pipeline(self) -> None:
        if not self._audio_buffer or self._audio_start is None:
            log.warn("gatekeeper.audio.empty", trace_id=self.trace_id)
            return

        try:
            transcript = await self._transcribe()
        except Exception as exc:  # noqa: BLE001 — error logged below
            log.error("gatekeeper.stt.error", trace_id=self.trace_id, error=str(exc))
            return

        if not transcript:
            log.warn("gatekeeper.transcript.empty", trace_id=self.trace_id)
            return
        log.info("gatekeeper.transcript", trace_id=self.trace_id, text=transcript)

        uid = await self._resolve_uid()
        endpoint = f"voice-pe:{self.client_id or 'unknown'}"
        response = await self._hermes.converse(
            text=transcript,
            uid=uid,
            endpoint=endpoint,
            trace_id=self.trace_id,
        )
        if not response:
            log.warn("gatekeeper.hermes.empty", trace_id=self.trace_id)
            return
        log.info("gatekeeper.response", trace_id=self.trace_id, length=len(response))

        try:
            await self._synthesize_and_stream(response)
        except Exception as exc:  # noqa: BLE001
            log.error("gatekeeper.tts.error", trace_id=self.trace_id, error=str(exc))
            return

        log.info("gatekeeper.session.close", trace_id=self.trace_id)

    async def _resolve_uid(self) -> str:
        """Phase 2 speaker resolution. Falls back to default_uid on any
        gap (feature disabled, no enrolments, model not loaded, empty
        buffer, embedding extraction failure). The resolver itself is
        in `speaker.py`; this method orchestrates the pieces and keeps
        the conversation pipeline working when the ML path is absent."""
        if not settings.speaker_id_enabled:
            return settings.default_uid
        extractor = get_extractor()
        if extractor is None or self._audio_start is None or not self._audio_buffer:
            return settings.default_uid
        pcm = b"".join(c.audio for c in self._audio_buffer)
        rate = self._audio_start.rate
        width = self._audio_start.width
        channels = self._audio_start.channels
        try:
            query = await asyncio.to_thread(
                extractor.extract, pcm, rate=rate, width=width, channels=channels
            )
        except Exception as exc:  # noqa: BLE001 — extraction errors degrade gracefully
            log.warn("gatekeeper.speaker.extract_error", trace_id=self.trace_id, error=str(exc))
            return settings.default_uid
        candidates = await asyncio.to_thread(list_embeddings, settings.oscar_db_path)
        uid, match = resolve_speaker(
            query,
            candidates,
            threshold=settings.speaker_id_threshold,
            default_uid=settings.default_uid,
        )
        if match is not None:
            log.info(
                "gatekeeper.speaker.match",
                trace_id=self.trace_id,
                uid=uid,
                best_uid=match.uid,
                score=round(match.score, 4),
                above_threshold=match.above_threshold,
            )
        if uid != settings.default_uid:
            await asyncio.to_thread(touch_last_seen, settings.oscar_db_path, uid)
        return uid

    async def _transcribe(self) -> str:
        assert self._audio_start is not None
        async with AsyncClient.from_uri(settings.whisper_uri) as client:
            await client.write_event(Transcribe(language=None).event())
            await client.write_event(self._audio_start.event())
            for chunk in self._audio_buffer:
                await client.write_event(chunk.event())
            await client.write_event(AudioStop().event())
            while True:
                evt = await client.read_event()
                if evt is None:
                    return ""
                if Transcript.is_type(evt.type):
                    return Transcript.from_event(evt).text

    async def _synthesize_and_stream(self, text: str) -> None:
        await synthesize_to_writer(settings.piper_uri, text, self.write_event)

"""Tests for the reverse enroll-stash (#376): the gatekeeper-side store and the
handler capture path.

The store mirrors uid_stash; the handler, when an enroll request is active and
speaker-ID is on, captures each onboarding turn's PCM as a sample and after N
enrols the averaged embedding in-process. Tests use a real sqlite db (the three
relevant tables replayed) and a stub extractor that returns a fixed 256-d vector.
"""

from __future__ import annotations

import dataclasses
import sqlite3
import struct
from unittest.mock import AsyncMock

from gatekeeper import enroll_stash
from gatekeeper import handler as handler_mod
from gatekeeper.handler import GatekeeperHandler
from wyoming.asr import Transcribe
from wyoming.audio import AudioChunk, AudioStart, AudioStop

_SCHEMA = """
CREATE TABLE voice_uid_stash (
    transcript TEXT PRIMARY KEY,
    uid        TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE enroll_requests (
    uid            TEXT PRIMARY KEY,
    status         TEXT NOT NULL DEFAULT 'pending',
    target_samples INTEGER NOT NULL DEFAULT 3,
    collected      INTEGER NOT NULL DEFAULT 0,
    result         TEXT,
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE voice_embeddings (
    uid          TEXT PRIMARY KEY,
    embedding    BLOB NOT NULL,
    sample_count INTEGER NOT NULL,
    enrolled_via TEXT NOT NULL,
    enrolled_at  TEXT,
    last_seen_at TEXT
);
"""

# A unit-norm-able 256-d float32 vector (1024 bytes) the stub extractor returns.
_EMBEDDING = struct.pack("<256f", *([0.5] * 256))


def _db(tmp_path) -> str:
    path = str(tmp_path / "solaris.db")
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA)
    conn.commit()
    conn.close()
    return path


class _StubInfo:
    def event(self):
        return "info-event"


class _StubExtractor:
    def extract(self, pcm, *, rate, width, channels):
        return _EMBEDDING


def _audio_events():
    return [
        AudioStart(rate=16000, width=2, channels=1).event(),
        AudioChunk(rate=16000, width=2, channels=1, audio=b"\x00\x00").event(),
        AudioStop().event(),
    ]


async def _turn(handler: GatekeeperHandler):
    for ev in [Transcribe().event(), *_audio_events()]:
        await handler.handle_event(ev)


def _new_handler(
    db: str, monkeypatch, *, extractor: object | None
) -> GatekeeperHandler:
    monkeypatch.setattr(
        handler_mod,
        "settings",
        dataclasses.replace(handler_mod.settings, solaris_db_path=db),
    )
    monkeypatch.setattr(handler_mod, "get_extractor", lambda: extractor)
    h = GatekeeperHandler(None, None, _StubInfo())
    h.write_event = AsyncMock()
    h._transcribe = AsyncMock(return_value="lena")
    h._resolve_uid = AsyncMock(return_value="guest")
    return h


# --- store ------------------------------------------------------------------


def test_claim_marks_capturing_and_returns_row(tmp_path):
    db = _db(tmp_path)
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO enroll_requests (uid) VALUES ('lena')")
    conn.commit()
    conn.close()
    req = enroll_stash.claim_active_request(db)
    assert req is not None and req.uid == "lena" and req.target_samples == 3
    # Now marked capturing.
    conn = sqlite3.connect(db)
    status = conn.execute(
        "SELECT status FROM enroll_requests WHERE uid='lena'"
    ).fetchone()[0]
    conn.close()
    assert status == "capturing"


def test_claim_ignores_stale_request(tmp_path):
    db = _db(tmp_path)
    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO enroll_requests (uid, created_at) VALUES ('lena', datetime('now', ?))",
        (f"-{enroll_stash.ENROLL_TTL_SECONDS + 30} seconds",),
    )
    conn.commit()
    conn.close()
    assert enroll_stash.claim_active_request(db) is None


def test_claim_ignores_terminal_request(tmp_path):
    db = _db(tmp_path)
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO enroll_requests (uid, status) VALUES ('lena', 'done')")
    conn.commit()
    conn.close()
    assert enroll_stash.claim_active_request(db) is None


def test_claim_missing_db_is_none(tmp_path):
    assert enroll_stash.claim_active_request(str(tmp_path / "absent.db")) is None


def test_increment_and_finish(tmp_path):
    db = _db(tmp_path)
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO enroll_requests (uid) VALUES ('lena')")
    conn.commit()
    conn.close()
    assert enroll_stash.increment_collected(db, "lena") == 1
    assert enroll_stash.increment_collected(db, "lena") == 2
    enroll_stash.finish_request(db, "lena", ok=True, result="3")
    conn = sqlite3.connect(db)
    status = conn.execute(
        "SELECT status FROM enroll_requests WHERE uid='lena'"
    ).fetchone()[0]
    conn.close()
    assert status == "done"


def test_accumulator_take_clears(tmp_path):
    enroll_stash.add_embedding("zz", _EMBEDDING)
    assert enroll_stash.add_embedding("zz", _EMBEDDING) == 2
    assert len(enroll_stash.take_embeddings("zz")) == 2
    assert enroll_stash.take_embeddings("zz") == []  # cleared


# --- handler capture path ---------------------------------------------------


async def test_three_turns_enroll_in_process(tmp_path, monkeypatch):
    db = _db(tmp_path)
    enroll_stash.take_embeddings("lena")  # isolate the process-global buffer
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO enroll_requests (uid, target_samples) VALUES ('lena', 3)")
    conn.commit()
    conn.close()

    for _ in range(3):
        await _turn(_new_handler(db, monkeypatch, extractor=_StubExtractor()))

    conn = sqlite3.connect(db)
    status, collected = conn.execute(
        "SELECT status, collected FROM enroll_requests WHERE uid='lena'"
    ).fetchone()
    emb = conn.execute(
        "SELECT enrolled_via, sample_count FROM voice_embeddings WHERE uid='lena'"
    ).fetchone()
    conn.close()
    assert status == "done"
    assert collected == 3
    assert emb is not None and emb[0] == "voice" and emb[1] == 3


async def test_no_enroll_when_speaker_id_off(tmp_path, monkeypatch):
    db = _db(tmp_path)
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO enroll_requests (uid) VALUES ('lena')")
    conn.commit()
    conn.close()

    # Extractor None == speaker-ID off: the gatekeeper never picks up the
    # request, so it stays pending for the engine side to time out.
    await _turn(_new_handler(db, monkeypatch, extractor=None))

    conn = sqlite3.connect(db)
    status, collected = conn.execute(
        "SELECT status, collected FROM enroll_requests WHERE uid='lena'"
    ).fetchone()
    n_emb = conn.execute("SELECT COUNT(*) FROM voice_embeddings").fetchone()[0]
    conn.close()
    assert status == "pending"
    assert collected == 0
    assert n_emb == 0


async def test_no_active_request_is_noop(tmp_path, monkeypatch):
    db = _db(tmp_path)  # enroll_requests empty
    await _turn(_new_handler(db, monkeypatch, extractor=_StubExtractor()))
    conn = sqlite3.connect(db)
    n = conn.execute("SELECT COUNT(*) FROM voice_embeddings").fetchone()[0]
    conn.close()
    assert n == 0

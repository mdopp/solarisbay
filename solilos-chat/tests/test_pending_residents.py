"""Tests for the registration flow (#376): the pending_residents store, the
`register_pending_resident` tool, and a single-head migration check that the
0013 migration applies cleanly on top of 0012.

The gatekeeper /enrol call is mocked with a local aiohttp app (as in
test_enrol), so we assert the tool enrols then files a pending row on success,
and files nothing when enrolment fails — and that raw audio never reaches a log
line. The migration is driven through alembic against a temp sqlite db.
"""

from __future__ import annotations

import base64
import json
import logging
from pathlib import Path

import pytest
from aiohttp import web
from alembic.config import Config
from alembic.script import ScriptDirectory

from solilos_chat import pending_residents_store
from solilos_chat.engine.tools.register import build_register_tools

_DB_DIR = Path(__file__).resolve().parents[2] / "database"
_SAMPLE = base64.b64encode(b"\x00\x01" * 16).decode()

# The schema migration 0013 creates, replayed locally so the store test runs
# against a real sqlite db without alembic (the migration itself is exercised by
# test_migration_applies_on_single_head below).
_SCHEMA = """
CREATE TABLE pending_residents (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  uid          TEXT NOT NULL,
  display_name TEXT NOT NULL,
  status       TEXT NOT NULL DEFAULT 'pending',
  enrolled     INTEGER NOT NULL DEFAULT 0,
  requested_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def _db(tmp_path) -> str:
    import sqlite3

    path = str(tmp_path / "solilos.db")
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA)
    conn.commit()
    conn.close()
    return path


def test_store_writes_and_reads_a_pending_request(tmp_path):
    db = _db(tmp_path)
    rid = pending_residents_store.add_pending_resident(
        db, uid="lena", display_name="Lena", enrolled=True
    )
    assert rid > 0
    rows = pending_residents_store.list_pending_residents(db)
    assert len(rows) == 1
    row = rows[0]
    assert row["uid"] == "lena"
    assert row["display_name"] == "Lena"
    assert row["status"] == "pending"
    assert row["enrolled"] == 1


def test_store_reads_empty_when_db_missing(tmp_path):
    assert (
        pending_residents_store.list_pending_residents(str(tmp_path / "absent.db"))
        == []
    )


@pytest.fixture
async def gatekeeper(aiohttp_client):
    """A stub gatekeeper whose /enrol reply the test can queue."""
    reply = {"status": 200, "body": {"ok": True, "uid": "lena", "samples_used": 3}}

    async def enrol(request: web.Request) -> web.Response:
        await request.json()
        return web.json_response(reply["body"], status=reply["status"])

    app = web.Application()
    app.router.add_post("/enrol", enrol)
    client = await aiohttp_client(app)
    return str(client.make_url("")).rstrip("/"), reply


async def test_register_enrols_then_files_pending_on_success(tmp_path, gatekeeper):
    base, _ = gatekeeper
    db = _db(tmp_path)
    (tool,) = build_register_tools(db, base, gatekeeper_token="s3cret")

    out = json.loads(
        await tool.handler(
            {
                "uid": "lena",
                "display_name": "Lena",
                "samples": [_SAMPLE, _SAMPLE, _SAMPLE],
            }
        )
    )
    assert out["ok"] is True
    assert out["uid"] == "lena"
    assert out["status"] == "pending"

    rows = pending_residents_store.list_pending_residents(db)
    assert len(rows) == 1
    assert rows[0]["uid"] == "lena"
    assert rows[0]["enrolled"] == 1


async def test_register_files_nothing_on_enrol_failure(tmp_path, gatekeeper):
    base, reply = gatekeeper
    reply["status"] = 422
    reply["body"] = {"ok": False, "reason": "not_enough_usable_samples"}
    db = _db(tmp_path)
    (tool,) = build_register_tools(db, base)

    out = json.loads(
        await tool.handler(
            {"uid": "lena", "display_name": "Lena", "samples": [_SAMPLE]}
        )
    )
    assert out == {"ok": False, "reason": "not_enough_usable_samples"}
    assert pending_residents_store.list_pending_residents(db) == []


async def test_register_rejects_missing_display_name(tmp_path, gatekeeper):
    base, _ = gatekeeper
    db = _db(tmp_path)
    (tool,) = build_register_tools(db, base)
    out = json.loads(
        await tool.handler({"uid": "lena", "display_name": "  ", "samples": [_SAMPLE]})
    )
    assert out == {"ok": False, "reason": "missing_display_name"}
    assert pending_residents_store.list_pending_residents(db) == []


async def test_register_does_not_log_audio(tmp_path, gatekeeper, caplog):
    base, _ = gatekeeper
    db = _db(tmp_path)
    (tool,) = build_register_tools(db, base)
    with caplog.at_level(logging.DEBUG):
        await tool.handler(
            {
                "uid": "lena",
                "display_name": "Lena",
                "samples": [_SAMPLE, _SAMPLE, _SAMPLE],
            }
        )
    assert _SAMPLE not in caplog.text


def _alembic_config(db_url: str) -> Config:
    cfg = Config(str(_DB_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(_DB_DIR / "migrations"))
    cfg.set_main_option("sqlalchemy.url", db_url)
    return cfg


def test_migration_chain_has_a_single_head():
    script = ScriptDirectory.from_config(_alembic_config("sqlite://"))
    heads = script.get_heads()
    assert heads == ["0013_pending_residents"], heads


def test_migration_applies_on_single_head(tmp_path):
    import sqlite3

    from alembic import command

    db_path = tmp_path / "migrated.db"
    command.upgrade(_alembic_config(f"sqlite:///{db_path}"), "head")

    conn = sqlite3.connect(db_path)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(pending_residents)")}
    finally:
        conn.close()
    assert {"id", "uid", "display_name", "status", "enrolled", "requested_at"} <= cols

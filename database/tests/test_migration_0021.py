"""Migration 0021 (device_tokens) applies onto the 0020 head (#717).

An alembic-in-the-loop test lives HERE, in database/ — the solaris-chat tests
must never import alembic (CI runs that package in a clean env without it). This
runs `alembic upgrade head` against a throwaway sqlite file and asserts the
device_tokens table + its hash/owner indexes + the token_hash UNIQUE constraint
landed, and that the migration chain still has a single linear head.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

_ROOT = Path(__file__).resolve().parent.parent


def _cfg(db_path: str) -> Config:
    cfg = Config(str(_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    return cfg


def test_single_linear_head(tmp_path):
    # The chain head advances as later migrations land (0022 adds
    # wartung_seen_updates, #788); assert a SINGLE linear head, not its name.
    heads = ScriptDirectory.from_config(_cfg(str(tmp_path / "x.db"))).get_heads()
    assert len(heads) == 1


def test_upgrade_creates_device_tokens(tmp_path):
    db = str(tmp_path / "solaris.db")
    command.upgrade(_cfg(db), "head")
    conn = sqlite3.connect(db)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(device_tokens)")}
        assert cols == {
            "id",
            "owner_uid",
            "token_hash",
            "label",
            "created",
            "last_used",
            "revoked",
        }
        indexes = {r[1] for r in conn.execute("PRAGMA index_list(device_tokens)")}
        assert "device_tokens_hash_idx" in indexes
        assert "device_tokens_owner_idx" in indexes

        # token_hash UNIQUE: a duplicate insert must fail.
        conn.execute(
            "INSERT INTO device_tokens (id, owner_uid, token_hash)"
            " VALUES ('a', 'mdopp', 'HASH')"
        )
        try:
            conn.execute(
                "INSERT INTO device_tokens (id, owner_uid, token_hash)"
                " VALUES ('b', 'lena', 'HASH')"
            )
            raised = False
        except sqlite3.IntegrityError:
            raised = True
        assert raised
    finally:
        conn.close()

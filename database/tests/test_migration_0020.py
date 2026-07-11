"""Migration 0020 (push_subscriptions) applies onto the 0019 head (#713).

An alembic-in-the-loop test lives HERE, in database/ — the solaris-chat tests
must never import alembic (CI runs that package in a clean env without it). This
runs `alembic upgrade head` against a throwaway sqlite file and asserts the
push_subscriptions table + its owner index + the endpoint UNIQUE constraint
landed.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from alembic import command
from alembic.config import Config

_ROOT = Path(__file__).resolve().parent.parent


def _upgrade(db_path: str) -> None:
    cfg = Config(str(_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(_ROOT / "migrations"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db_path}")
    command.upgrade(cfg, "head")


def test_upgrade_creates_push_subscriptions(tmp_path):
    db = str(tmp_path / "solaris.db")
    _upgrade(db)
    conn = sqlite3.connect(db)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(push_subscriptions)")}
        assert cols == {
            "id",
            "owner_uid",
            "endpoint",
            "p256dh",
            "auth",
            "user_agent",
            "created",
            "last_ok",
        }
        indexes = {r[1] for r in conn.execute("PRAGMA index_list(push_subscriptions)")}
        assert "push_subscriptions_owner_idx" in indexes

        # endpoint UNIQUE: a duplicate insert must fail.
        conn.execute(
            "INSERT INTO push_subscriptions (id, owner_uid, endpoint, p256dh, auth)"
            " VALUES ('a', 'mdopp', 'https://push/1', 'p', 'a')"
        )
        try:
            conn.execute(
                "INSERT INTO push_subscriptions (id, owner_uid, endpoint, p256dh,"
                " auth) VALUES ('b', 'lena', 'https://push/1', 'p', 'a')"
            )
            raised = False
        except sqlite3.IntegrityError:
            raised = True
        assert raised
    finally:
        conn.close()

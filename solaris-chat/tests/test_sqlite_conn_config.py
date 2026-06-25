"""solaris.db connection config — WAL journal + busy_timeout (#600).

Concurrent chat turns (and an overlapping background ingest) hit
`sqlite3.OperationalError: database is locked` because SQLite is
single-writer. The connect helpers that open solaris.db must set
`journal_mode=WAL` (readers + one writer coexist; persisted in the db
header) and a `busy_timeout` (a blocked writer waits instead of raising).
"""

from __future__ import annotations

import sqlite3
import time

import pytest

from solaris_chat import (
    enroll_requests_store,
    mentions_store,
    pending_residents_store,
    topics_store,
    trace_store,
    voice_uid_stash,
)
from solaris_chat.engine import scheduler
from solaris_chat.engine.knowledge import projection

# Each entry is (module, connect-helper attribute) for a helper that opens
# solaris.db on a path shared by chat turns and/or the ingest writer.
_HELPERS = [
    (trace_store, "_connect"),
    (topics_store, "_connect"),
    (mentions_store, "_connect"),
    (pending_residents_store, "_connect"),
    (enroll_requests_store, "_connect"),
    (voice_uid_stash, "_connect"),
    (scheduler, "_conn"),
    (projection, "open_conn"),
]


def _db(tmp_path) -> str:
    return str(tmp_path / "solaris.db")


@pytest.mark.parametrize(
    "module,attr", _HELPERS, ids=lambda v: getattr(v, "__name__", v)
)
def test_connect_helper_sets_wal_and_busy_timeout(module, attr, tmp_path):
    connect = getattr(module, attr)
    conn = connect(_db(tmp_path))
    try:
        journal = conn.execute("PRAGMA journal_mode").fetchone()[0]
        busy = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    finally:
        conn.close()
    assert journal.lower() == "wal"
    assert busy == 5000


def test_second_writer_waits_under_busy_timeout(tmp_path):
    """busy_timeout in effect: a second connection contending for the write
    lock *waits* rather than failing on the first attempt (the #600 symptom is
    the immediate raise). We hold the lock so it still times out — but only
    after blocking for ~the timeout, which proves busy_timeout is applied."""
    path = _db(tmp_path)
    a = trace_store._connect(path)
    a.execute("CREATE TABLE t (x INTEGER)")
    a.commit()
    a.execute("BEGIN IMMEDIATE")  # hold the write lock
    a.execute("INSERT INTO t VALUES (1)")

    b = trace_store._connect(path)
    b.execute("PRAGMA busy_timeout = 300")  # keep the test fast
    started = time.monotonic()
    try:
        with pytest.raises(sqlite3.OperationalError):
            b.execute("BEGIN IMMEDIATE")
        waited = time.monotonic() - started
        assert waited >= 0.25  # it blocked on the lock instead of raising at once
    finally:
        a.rollback()
        a.close()
        b.close()

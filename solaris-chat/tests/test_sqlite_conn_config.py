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
    notes_index,
    pending_residents_store,
    topics_store,
    trace_store,
    voice_uid_stash,
)
from solaris_chat.engine import approvals, scheduler, updates
from solaris_chat.engine.knowledge import projection

# Each entry is (module, connect-helper attribute, expected busy_timeout ms) for a
# helper that opens solaris.db on a path shared by chat turns and/or the ingest
# writer. The ingest-write path + the boot pollers that contend with it run at
# 10s (matching the chat store) so a mid-ingest write waits out a WAL checkpoint
# instead of raising "database is locked" and dropping the row (#835); the rest
# stay at the 5s #600 baseline.
_HELPERS = [
    (trace_store, "_connect", 5000),
    (topics_store, "_connect", 5000),
    (mentions_store, "_connect", 5000),
    (pending_residents_store, "_connect", 5000),
    (enroll_requests_store, "_connect", 5000),
    (voice_uid_stash, "_connect", 5000),
    (scheduler, "_conn", 5000),
    (projection, "open_conn", 10000),
    (updates, "_conn", 10000),
    (approvals, "_conn", 10000),
]


def _db(tmp_path) -> str:
    return str(tmp_path / "solaris.db")


@pytest.mark.parametrize(
    "module,attr,expected_busy", _HELPERS, ids=lambda v: getattr(v, "__name__", v)
)
def test_connect_helper_sets_wal_and_busy_timeout(
    module, attr, expected_busy, tmp_path
):
    connect = getattr(module, attr)
    conn = connect(_db(tmp_path))
    try:
        journal = conn.execute("PRAGMA journal_mode").fetchone()[0]
        busy = conn.execute("PRAGMA busy_timeout").fetchone()[0]
    finally:
        conn.close()
    assert journal.lower() == "wal"
    assert busy == expected_busy


def test_notes_index_backfill_leaves_db_in_wal(tmp_path):
    """The full-vault FTS backfill (#830) opens its own long-lived write
    connection; without WAL (#835) it races the boot ingest to "database is
    locked". A backfill over an empty vault still runs and must leave the db in
    WAL so the header is set for every later connection."""
    root = tmp_path / "notes"
    root.mkdir()
    db = _db(tmp_path)
    notes_index.backfill(db, str(root))

    conn = sqlite3.connect(db)
    try:
        journal = conn.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        conn.close()
    assert journal.lower() == "wal"


def test_ingest_writer_waits_out_a_mid_ingest_poller_write(tmp_path):
    """Regression for #835: while the ingest writer (projection.open_conn) holds
    the write lock, a poller write must WAIT and land, not raise "database is
    locked" and drop its row. With the pre-fix 5s poller timeout a >5s hold would
    raise; here we prove the poller's connection blocks past a short hold instead
    of failing on the first contended attempt."""
    path = _db(tmp_path)
    writer = projection.open_conn(path)
    writer.execute("CREATE TABLE seen (id TEXT)")
    writer.commit()
    writer.execute("BEGIN IMMEDIATE")  # ingest holds the write lock
    writer.execute("INSERT INTO seen VALUES ('song-1')")

    poller = updates._conn(path)
    poller.execute("PRAGMA busy_timeout = 300")  # keep the test fast
    started = time.monotonic()
    try:
        with pytest.raises(sqlite3.OperationalError):
            poller.execute("BEGIN IMMEDIATE")
        waited = time.monotonic() - started
        assert waited >= 0.25  # it blocked on the lock instead of raising at once
    finally:
        writer.commit()
        writer.close()
        poller.close()


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

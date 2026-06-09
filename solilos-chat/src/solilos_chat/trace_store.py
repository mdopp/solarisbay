"""SQLite access to persisted per-message LLM traces.

Reads/writes the `session_traces` table (migration 0007) in solilos.db. Phase 2
of the trace sidecar (#306): at turn time the server pulls the trace proxy's
Ollama calls for that turn's window, assigns them a stable per-message
`trace_id`, and persists each step here in order — so reopening a chat shows the
same trace, persistently. `detail_id` links a step back to the proxy's
`/__traces__/<id>` exact-content record.

Sync sqlite3, like `topics_store` / `mentions_store`: each op is
millisecond-cheap. If solilos.db or the table is missing (the schema-init
sidecar hasn't migrated yet), writes/reads degrade to no-op/empty rather than
erroring — the trace panel just shows nothing until the migration lands.

Scoping is per-resident (D3): every row carries the recording `owner_uid` and
every read/write is filtered by it, so a resident never sees another resident's
trace.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

# The per-step columns persisted from a proxy trace record (besides the keys +
# owner_uid). Order matches the INSERT below.
_STEP_FIELDS = (
    "model",
    "wall_s",
    "prompt_tokens",
    "completion_tokens",
    "context_free",
    "finish_reason",
    "n_tools",
    "detail_id",
)


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def persist_trace(
    db_path: str,
    session_id: str,
    trace_id: str,
    owner_uid: str,
    steps: list[dict[str, Any]],
) -> None:
    """Persist a turn's ordered LLM steps under `trace_id`.

    Each step is a proxy trace record (the `/__traces__` shape); `step_order` is
    its index in `steps`. Re-persisting the same `trace_id` replaces the prior
    rows (idempotent on a retried turn). No-op when there's nothing to record,
    the DB is missing, or the table hasn't been migrated yet.
    """
    if not steps or not Path(db_path).exists():
        return
    rows = [
        (
            session_id,
            trace_id,
            i,
            owner_uid,
            *(step.get(f) for f in _STEP_FIELDS),
        )
        for i, step in enumerate(steps)
    ]
    try:
        with _connect(db_path) as conn:
            conn.execute(
                "DELETE FROM session_traces WHERE session_id = ? AND trace_id = ? "
                "AND owner_uid = ?",
                (session_id, trace_id, owner_uid),
            )
            conn.executemany(
                """
                INSERT INTO session_traces
                  (session_id, trace_id, step_order, owner_uid,
                   model, wall_s, prompt_tokens, completion_tokens,
                   context_free, finish_reason, n_tools, detail_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
            conn.commit()
    except sqlite3.OperationalError:
        return


def list_session_trace(
    db_path: str, session_id: str, owner_uid: str
) -> list[dict[str, Any]]:
    """The resident's persisted trace for a chat, in turn-then-step order.

    Ordered by insertion (`rowid`): turns were persisted chronologically and each
    turn's steps in `step_order`, so the reopened trace matches the live order.
    Each item carries the keys + the persisted step fields. Empty when the
    DB/table is missing or the chat has no trace.
    """
    if not Path(db_path).exists():
        return []
    try:
        with _connect(db_path) as conn:
            rows = conn.execute(
                """
                SELECT trace_id, step_order, model, wall_s, prompt_tokens,
                       completion_tokens, context_free, finish_reason,
                       n_tools, detail_id
                  FROM session_traces
                 WHERE session_id = ? AND owner_uid = ?
                 ORDER BY rowid
                """,
                (session_id, owner_uid),
            ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [dict(r) for r in rows]

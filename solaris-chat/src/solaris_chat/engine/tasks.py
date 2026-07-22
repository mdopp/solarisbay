"""Aufgaben (to-do) — the general task list (#todo, plan slice 1).

A task is a projection-only `task` entity carrying facts under the single `task`
source: `status` (open|done|dismissed), `title_text`, `task_source`
(manual|chat|music-import|document), `created`, and an optional ISO `due`. There
is no per-task markdown — the entity + facts are the whole record, so a status
toggle is a cheap fact replace (mirrors the projection-only album of the music
import).

Owner scope is the entity's `resident_uid`: the caller sees their own tasks plus
the shared household pool, never another resident's (matches
`projection.entity_facts`). A dated task also becomes a calendar entry via the
nightly deadlines sync (`document_deadlines_sync`).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from solaris_chat.engine.knowledge import projection
from solaris_chat.engine.knowledge.records import ConceptRecord
from solaris_chat.engine.knowledge.writer import write_concept

_SOURCE = "task"
_OPEN = "open"
_DONE = "done"
_DISMISSED = "dismissed"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def create_task(
    *,
    db_path: str,
    notes_dir: str,
    uid: str,
    title: str,
    due: str = "",
    task_source: str = "manual",
) -> str:
    """Create an open task and return its entity id. `due` is an ISO date (or "")."""
    title = (title or "").strip()
    if not title:
        raise ValueError("task title is empty")
    key = f"task:{uid}:{uuid.uuid4().hex[:12]}"
    facts: list[tuple[str, str, float | None]] = [
        ("status", _OPEN, 1.0),
        ("title_text", title, 1.0),
        ("task_source", task_source, 1.0),
        ("created", _now_iso(), 1.0),
    ]
    if due.strip():
        facts.append(("due", due.strip(), 1.0))
    rec = ConceptRecord(
        type="task",
        title=title,
        source=_SOURCE,
        external_id=key,
        identity_key=key,
        resident=uid,
        facts=facts,
        projection_only=True,
    )
    return write_concept(
        rec, db_path=db_path, notes_dir=notes_dir, ingesting_uid=uid
    ).ref_id


def set_status(*, db_path: str, uid: str, entity_id: str, status: str) -> bool:
    """Set a task's status (open|done|dismissed); rewrites the `task`-source facts.
    Returns False if the task isn't visible to the caller."""
    if status not in (_OPEN, _DONE, _DISMISSED):
        raise ValueError(f"bad task status: {status!r}")
    conn = projection.open_conn(db_path)
    try:
        owner = conn.execute(
            "SELECT resident_uid FROM entities WHERE id = ? AND type = 'task'"
            " AND resident_uid IN (?, ?)",
            (entity_id, uid, projection.SHARED_UID),
        ).fetchone()
        if owner is None:
            return False
        current = {
            f["predicate"]: (f["value"], f["confidence"])
            for f in projection.entity_facts(conn, entity_id, uid)
        }
        current["status"] = (status, 1.0)
        # Stamp when it left `open` so a recently-resolved task can still surface
        # (the `.task` filter shows open + resolved-in-the-last-week).
        if status == _OPEN:
            current.pop("resolved_at", None)
        else:
            current["resolved_at"] = (_now_iso(), 1.0)
        projection.replace_facts(
            conn,
            subject_entity_id=entity_id,
            resident_uid=owner["resident_uid"],
            source=_SOURCE,
            facts=[(p, v, c) for p, (v, c) in current.items()],
        )
        conn.commit()
    finally:
        conn.close()
    return True


def update(*, db_path: str, uid: str, entity_id: str, title: str, due: str) -> bool:
    """Correct a task's title/due; rewrites the `task`-source facts, preserving
    status and the rest. Returns False if the task isn't visible to the caller."""
    title = (title or "").strip()
    if not title:
        raise ValueError("task title is empty")
    conn = projection.open_conn(db_path)
    try:
        owner = conn.execute(
            "SELECT resident_uid FROM entities WHERE id = ? AND type = 'task'"
            " AND resident_uid IN (?, ?)",
            (entity_id, uid, projection.SHARED_UID),
        ).fetchone()
        if owner is None:
            return False
        current = {
            f["predicate"]: (f["value"], f["confidence"])
            for f in projection.entity_facts(conn, entity_id, uid)
        }
        current["title_text"] = (title, 1.0)
        if due.strip():
            current["due"] = (due.strip(), 1.0)
        else:
            current.pop("due", None)
        projection.replace_facts(
            conn,
            subject_entity_id=entity_id,
            resident_uid=owner["resident_uid"],
            source=_SOURCE,
            facts=[(p, v, c) for p, (v, c) in current.items()],
        )
        conn.execute(
            "UPDATE entities SET canonical_name = ? WHERE id = ?", (title, entity_id)
        )
        conn.commit()
    finally:
        conn.close()
    return True


def list_tasks(
    db_path: str, uid: str, *, include_done: bool = False
) -> list[dict[str, Any]]:
    """Tasks visible to the caller (own ∪ household). Open first, dated by due date,
    then most-recent. `include_done` also returns done/dismissed tasks."""
    conn = projection.open_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT id, canonical_name FROM entities"
            " WHERE type = 'task' AND resident_uid IN (?, ?)",
            (uid, projection.SHARED_UID),
        ).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            facts = {
                f["predicate"]: f["value"]
                for f in projection.entity_facts(conn, r["id"], uid)
            }
            status = facts.get("status", _OPEN)
            if not include_done and status != _OPEN:
                continue
            out.append(
                {
                    "id": r["id"],
                    "title": facts.get("title_text", r["canonical_name"]),
                    "status": status,
                    "due": facts.get("due", ""),
                    "source": facts.get("task_source", "manual"),
                    "created": facts.get("created", ""),
                    "resolved_at": facts.get("resolved_at", ""),
                }
            )
    finally:
        conn.close()
    # Stable two-pass: newest first, then the primary ordering (open before
    # resolved; among those, dated-by-due before undated).
    out.sort(key=lambda t: t["created"], reverse=True)
    out.sort(key=lambda t: (t["status"] != _OPEN, t["due"] == "", t["due"]))
    return out

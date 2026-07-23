"""Cross-source person dedup + human-confirmed merge (#994, ADR 0010).

The persons/contacts SSOT (`entities` of `type='person'`) accretes duplicates:
the same human arrives from `.contacts`, a CalDAV sync, and chat `@`-mentions as
three separate entities. This module finds *likely* duplicates and, on explicit
confirmation, merges them onto one primary — re-pointing the secondary's
aliases/facts/event edges and recording an audit/undo trail.

Two invariants make this safe to ship (merging two humans is DESTRUCTIVE and
irreversible without care):

  1. **Conservative detection.** A candidate needs a shared *contact key* (a
     normalized phone or email — the strongest cross-source person signal) AND
     compatible names. A name-only match is never offered: two distinct "Anna
     Meyer"s must not be auto-merged. False-merge = irreversible data loss, so
     detection biases hard toward precision over recall.
  2. **Per-resident isolation.** Detection is scoped to ONE resident's own ∪
     shared-household persons (`resident_uid IN (uid, 'household')`), never
     across residents. A person private to resident A can never be offered as a
     merge target for resident B, so a merge can't leak A's person into B's
     scope. Cross-resident merge is a deliberate non-goal here (follow-up):
     mixing owners is the highest-risk case, so this slice is cross-SOURCE only.

Merge itself is never automatic: `find_merge_candidates` surfaces pairs for the
UI to confirm, `preview_merge` is a no-write dry-run, and only `merge_persons`
(called on explicit confirmation) mutates. Every merge writes a `person_merges`
row (the secondary's provenance + a snapshot of what moved) so it's auditable
and `undo_merge` can restore the secondary.
"""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from typing import Any

from solaris_chat.notes_search import SHARED_UID


# Contact facts that carry a person's identity across sources; a shared one of
# these is the merge signal. Kept small on purpose — an address or a birthday is
# too weak (shared households, common dates) to be a person key.
_CONTACT_PREDICATES = ("phone", "email")


def _normalize_name(name: str) -> str:
    """A person's name comparison key: casefold, drop punctuation, collapse
    whitespace. Empty when nothing alphanumeric survives — then names never
    match (an unnamed contact isn't a name signal)."""
    tokens = re.findall(r"[a-z0-9äöüß]+", name.casefold())
    return " ".join(tokens)


def _normalize_phone(phone: str) -> str:
    """Digits only, with a leading German 0 folded to +49 so `0177…` and
    `+49177…` are one key. Empty (unmatchable) below 6 digits — a fragment isn't
    a reliable person key."""
    digits = re.sub(r"\D", "", phone)
    if digits.startswith("00"):
        digits = digits[2:]
    elif digits.startswith("0"):
        digits = "49" + digits[1:]
    return digits if len(digits) >= 6 else ""


def _normalize_email(email: str) -> str:
    """Lowercased, trimmed. Empty (unmatchable) unless it looks like an address."""
    e = email.strip().lower()
    return e if "@" in e and "." in e.split("@")[-1] else ""


def _names_compatible(a: str, b: str) -> bool:
    """True when two normalized names could be the same person: equal, or one is
    a token-subset of the other (e.g. "anna" ⊂ "anna meyer"). Two disjoint full
    names ("anna meyer" vs "anna schmidt") are NOT compatible even sharing a
    contact key — that's the false-merge trap, so we bias to precision. An empty
    name is not a match on its own (needs the contact key AND a name signal)."""
    if not a or not b:
        return False
    ta, tb = set(a.split()), set(b.split())
    return ta == tb or ta <= tb or tb <= ta


def _person_keys(conn: sqlite3.Connection, entity_id: str) -> set[str]:
    """The normalized contact keys (phone/email) recorded for a person, across
    ALL sources — so a phone from `.contacts` and the same phone from CalDAV
    collide even though they were written under different `source`s."""
    keys: set[str] = set()
    for f in conn.execute(
        "SELECT predicate, value FROM facts WHERE subject_entity_id = ?",
        (entity_id,),
    ).fetchall():
        if f["predicate"] == "phone":
            k = _normalize_phone(f["value"])
            if k:
                keys.add("phone:" + k)
        elif f["predicate"] == "email":
            k = _normalize_email(f["value"])
            if k:
                keys.add("email:" + k)
    return keys


def _person_aliases(conn: sqlite3.Connection, entity_id: str) -> list[str]:
    return [
        r["alias"]
        for r in conn.execute(
            "SELECT alias FROM entity_aliases WHERE entity_id = ? ORDER BY alias",
            (entity_id,),
        ).fetchall()
    ]


def find_merge_candidates(conn: sqlite3.Connection, uid: str) -> list[dict[str, Any]]:
    """Likely-duplicate person pairs for `uid` (own ∪ shared household), for the
    UI to CONFIRM — never auto-merged.

    A pair is offered only when the two persons share a normalized contact key
    (phone/email) AND their names are compatible (`_names_compatible`). Scoped to
    one resident's own ∪ shared persons, so no cross-resident pair is ever
    surfaced. Each candidate is
    `{primary, secondary, reason, primary_name, secondary_name}` — the older
    entity id (lexicographically stable) is the primary (merge target)."""
    rows = conn.execute(
        "SELECT id, canonical_name, resident_uid FROM entities"
        " WHERE type = 'person' AND resident_uid IN (?, ?)"
        " ORDER BY id",
        (uid, SHARED_UID),
    ).fetchall()
    persons = [
        {
            "id": r["id"],
            "name": r["canonical_name"],
            "norm": _normalize_name(r["canonical_name"]),
            "resident_uid": r["resident_uid"],
            "keys": _person_keys(conn, r["id"]),
        }
        for r in rows
    ]
    seen: set[tuple[str, str]] = set()
    out: list[dict[str, Any]] = []
    for i, a in enumerate(persons):
        for b in persons[i + 1 :]:
            shared = a["keys"] & b["keys"]
            if not shared:
                continue
            if not _names_compatible(a["norm"], b["norm"]):
                continue
            # id order is the primary; keep the pair once.
            primary, secondary = sorted((a, b), key=lambda p: p["id"])
            pair = (primary["id"], secondary["id"])
            if pair in seen:
                continue
            seen.add(pair)
            out.append(
                {
                    "primary": primary["id"],
                    "secondary": secondary["id"],
                    "primary_name": primary["name"],
                    "secondary_name": secondary["name"],
                    "reason": sorted(shared),
                }
            )
    return out


def _owned_person(
    conn: sqlite3.Connection, entity_id: str, uid: str
) -> sqlite3.Row | None:
    """The person row iff the caller may act on it (own ∪ shared household).
    Owner-gates every mutation — the caller can never touch a person outside its
    scope, so a merge can't reach across residents."""
    return conn.execute(
        "SELECT id, canonical_name, resident_uid, source FROM entities"
        " WHERE id = ? AND type = 'person' AND resident_uid IN (?, ?)",
        (entity_id, uid, SHARED_UID),
    ).fetchone()


def preview_merge(
    conn: sqlite3.Connection, primary_id: str, secondary_id: str, uid: str
) -> dict[str, Any] | None:
    """A no-write dry-run of merging `secondary` into `primary`: what the merged
    person would carry. Owner-gated on BOTH persons (returns ``None`` if either
    is out of the caller's scope, so cross-resident is refused here too).

    Returns `{primary, secondary, name, aliases, facts, keys}` — the union of the
    two persons' aliases and their combined contact keys — for the confirmation
    card to show before the resident commits."""
    p = _owned_person(conn, primary_id, uid)
    s = _owned_person(conn, secondary_id, uid)
    if p is None or s is None or primary_id == secondary_id:
        return None
    aliases = sorted(
        set(_person_aliases(conn, primary_id))
        | set(_person_aliases(conn, secondary_id))
    )
    keys = sorted(_person_keys(conn, primary_id) | _person_keys(conn, secondary_id))
    facts = [
        {"predicate": f["predicate"], "value": f["value"], "source": f["source"]}
        for f in conn.execute(
            "SELECT predicate, value, source FROM facts"
            " WHERE subject_entity_id IN (?, ?) ORDER BY predicate, value",
            (primary_id, secondary_id),
        ).fetchall()
    ]
    return {
        "primary": primary_id,
        "secondary": secondary_id,
        "name": p["canonical_name"],
        "aliases": aliases,
        "facts": facts,
        "keys": keys,
    }


def _snapshot(conn: sqlite3.Connection, entity_id: str) -> dict[str, Any]:
    """A restorable snapshot of the secondary before merge: its aliases, facts,
    and event edges. Stored in the undo trail so `undo_merge` can recreate the
    entity and its rows."""
    return {
        "aliases": _person_aliases(conn, entity_id),
        "facts": [
            dict(r)
            for r in conn.execute(
                "SELECT id, resident_uid, predicate, value, confidence, source"
                " FROM facts WHERE subject_entity_id = ?",
                (entity_id,),
            ).fetchall()
        ],
        "event_entities": [
            dict(r)
            for r in conn.execute(
                "SELECT event_id, role FROM event_entities WHERE entity_id = ?",
                (entity_id,),
            ).fetchall()
        ],
    }


def merge_persons(
    conn: sqlite3.Connection,
    *,
    primary_id: str,
    secondary_id: str,
    uid: str,
) -> str | None:
    """Merge `secondary` into `primary` — call ONLY on explicit confirmation.

    Owner-gated on both persons (own ∪ shared household), so it refuses a
    cross-resident merge. Re-points the secondary's aliases, facts, and event
    edges onto the primary, records a `person_merges` audit/undo row (the
    secondary's provenance + a snapshot of what moved), then deletes the
    secondary entity. Returns the merge-record id, or ``None`` if either person
    is out of scope / the ids are equal.

    The caller commits. It also owns the OKF-file + `concepts`/embedding cleanup
    of the secondary (the projection here is rebuildable from the files)."""
    p = _owned_person(conn, primary_id, uid)
    s = _owned_person(conn, secondary_id, uid)
    if p is None or s is None or primary_id == secondary_id:
        return None

    snapshot = _snapshot(conn, secondary_id)
    merge_id = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO person_merges"
        " (id, primary_entity_id, secondary_entity_id, secondary_name,"
        "  secondary_resident_uid, secondary_source, snapshot, merged_by)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            merge_id,
            primary_id,
            secondary_id,
            s["canonical_name"],
            s["resident_uid"],
            s["source"],
            json.dumps(snapshot),
            uid,
        ),
    )

    # Re-point aliases (INSERT OR IGNORE dedups against the primary's own), plus
    # the secondary's canonical name so `@`-mentions of the old spelling resolve.
    for alias in [s["canonical_name"], *snapshot["aliases"]]:
        conn.execute(
            "INSERT OR IGNORE INTO entity_aliases (entity_id, alias) VALUES (?, ?)",
            (primary_id, alias),
        )
    conn.execute("DELETE FROM entity_aliases WHERE entity_id = ?", (secondary_id,))
    conn.execute(
        "UPDATE facts SET subject_entity_id = ? WHERE subject_entity_id = ?",
        (primary_id, secondary_id),
    )
    # Event edges: point at the primary, but INSERT OR IGNORE can't dedup via an
    # UPDATE, so delete-then-reinsert to respect the (event, entity, role) PK.
    for e in snapshot["event_entities"]:
        conn.execute(
            "INSERT OR IGNORE INTO event_entities (event_id, entity_id, role)"
            " VALUES (?, ?, ?)",
            (e["event_id"], primary_id, e["role"]),
        )
    conn.execute("DELETE FROM event_entities WHERE entity_id = ?", (secondary_id,))
    conn.execute("DELETE FROM entities WHERE id = ?", (secondary_id,))
    return merge_id


def undo_merge(conn: sqlite3.Connection, merge_id: str, uid: str) -> bool:
    """Reverse a merge from its audit row: recreate the secondary person and its
    facts/event edges from the snapshot. Owner-gated (the caller must have made
    the merge or share the secondary's scope). Returns ``False`` if the record is
    missing, already undone, or out of scope.

    This does NOT strip aliases/facts back off the primary (the merge folded them
    in and a re-ingest would re-add them anyway); it restores the secondary as a
    distinct entity so the false-merge is corrected. Aliases the secondary owned
    are re-attached to it."""
    row = conn.execute(
        "SELECT * FROM person_merges WHERE id = ? AND undone_at IS NULL",
        (merge_id,),
    ).fetchone()
    if row is None:
        return False
    if row["secondary_resident_uid"] not in (uid, SHARED_UID):
        return False
    if conn.execute(
        "SELECT 1 FROM entities WHERE id = ?", (row["secondary_entity_id"],)
    ).fetchone():
        return False  # already restored / id in use
    snapshot = json.loads(row["snapshot"])
    conn.execute(
        "INSERT INTO entities"
        " (id, type, canonical_name, resident_uid, source, content_hash)"
        " VALUES (?, 'person', ?, ?, ?, '')",
        (
            row["secondary_entity_id"],
            row["secondary_name"],
            row["secondary_resident_uid"],
            row["secondary_source"],
        ),
    )
    for alias in dict.fromkeys([row["secondary_name"], *snapshot["aliases"]]):
        conn.execute(
            "INSERT OR IGNORE INTO entity_aliases (entity_id, alias) VALUES (?, ?)",
            (row["secondary_entity_id"], alias),
        )
    # The merge MOVED the secondary's facts onto the primary (UPDATE, not copy),
    # so each snapshot fact id still exists under the primary — re-point it back.
    # A row that's since been deleted/re-ingested is re-inserted from the snapshot.
    for f in snapshot["facts"]:
        cur = conn.execute(
            "UPDATE facts SET subject_entity_id = ? WHERE id = ?",
            (row["secondary_entity_id"], f["id"]),
        )
        if cur.rowcount == 0:
            conn.execute(
                "INSERT INTO facts"
                " (id, subject_entity_id, resident_uid, predicate, value, confidence, source)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    f["id"],
                    row["secondary_entity_id"],
                    f["resident_uid"],
                    f["predicate"],
                    f["value"],
                    f["confidence"],
                    f["source"],
                ),
            )
    for e in snapshot["event_entities"]:
        conn.execute(
            "INSERT OR IGNORE INTO event_entities (event_id, entity_id, role)"
            " VALUES (?, ?, ?)",
            (e["event_id"], row["secondary_entity_id"], e["role"]),
        )
    conn.execute(
        "UPDATE person_merges SET undone_at = datetime('now') WHERE id = ?",
        (merge_id,),
    )
    return True

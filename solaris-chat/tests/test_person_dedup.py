"""Cross-source person dedup + human-confirmed merge (#994, ADR 0010).

Merging two `person` entities is DESTRUCTIVE + irreversible, so these tests pin
the safety invariants: detection is conservative (a shared contact key AND
compatible names — never name-only), merge is confirmation-gated + owner-scoped
(no cross-resident reach), and every merge is auditable/undoable via the
`person_merges` trail. The schema DDL mirrors migrations 0016 + 0028.
"""

from __future__ import annotations

import sqlite3

import pytest

from solaris_chat.engine.knowledge import person_dedup


# Mirrors migrations 0016_okf_knowledge_index + 0028_person_merges.
_SCHEMA = """
CREATE TABLE entities (
  id TEXT PRIMARY KEY, type TEXT NOT NULL, canonical_name TEXT NOT NULL,
  resident_uid TEXT NOT NULL, source TEXT NOT NULL, content_hash TEXT NOT NULL,
  updated TEXT NOT NULL DEFAULT (datetime('now')));
CREATE TABLE entity_aliases (
  entity_id TEXT NOT NULL, alias TEXT NOT NULL,
  PRIMARY KEY (entity_id, alias));
CREATE TABLE facts (
  id TEXT PRIMARY KEY, subject_entity_id TEXT, resident_uid TEXT NOT NULL,
  predicate TEXT NOT NULL, value TEXT NOT NULL, confidence REAL,
  source TEXT NOT NULL, timestamp TEXT NOT NULL DEFAULT (datetime('now')));
CREATE TABLE events (
  id TEXT PRIMARY KEY, ts TEXT NOT NULL, resident_uid TEXT NOT NULL,
  kind TEXT NOT NULL, source TEXT NOT NULL);
CREATE TABLE event_entities (
  event_id TEXT NOT NULL, entity_id TEXT NOT NULL, role TEXT NOT NULL,
  PRIMARY KEY (event_id, entity_id, role));
CREATE TABLE person_merges (
  id TEXT PRIMARY KEY, primary_entity_id TEXT NOT NULL,
  secondary_entity_id TEXT NOT NULL, secondary_name TEXT NOT NULL,
  secondary_resident_uid TEXT NOT NULL, secondary_source TEXT NOT NULL,
  snapshot TEXT NOT NULL, merged_by TEXT NOT NULL,
  merged_at TEXT NOT NULL DEFAULT (datetime('now')), undone_at TEXT);
"""


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.executescript(_SCHEMA)
    return c


def _person(conn, eid, name, resident, source="contact", facts=(), aliases=()):
    conn.execute(
        "INSERT INTO entities (id, type, canonical_name, resident_uid, source,"
        " content_hash) VALUES (?, 'person', ?, ?, ?, 'h')",
        (eid, name, resident, source),
    )
    conn.execute(
        "INSERT OR IGNORE INTO entity_aliases (entity_id, alias) VALUES (?, ?)",
        (eid, name),
    )
    for a in aliases:
        conn.execute(
            "INSERT OR IGNORE INTO entity_aliases (entity_id, alias) VALUES (?, ?)",
            (eid, a),
        )
    for i, (pred, val) in enumerate(facts):
        conn.execute(
            "INSERT INTO facts (id, subject_entity_id, resident_uid, predicate,"
            " value, confidence, source) VALUES (?, ?, ?, ?, ?, 1.0, ?)",
            (f"{eid}-{i}", eid, resident, pred, val, source),
        )


# --- normalization -----------------------------------------------------------


def test_phone_normalization_folds_german_prefix():
    assert person_dedup._normalize_phone(
        "0177 5524222"
    ) == person_dedup._normalize_phone("+49 177 5524222")
    assert person_dedup._normalize_phone(
        "0049177/5524222"
    ) == person_dedup._normalize_phone("+49 177 5524222")


def test_short_phone_is_not_a_key():
    assert person_dedup._normalize_phone("123") == ""


def test_email_normalization_lowercases():
    assert person_dedup._normalize_email("  Mdopp@Web.DE ") == "mdopp@web.de"
    assert person_dedup._normalize_email("not-an-email") == ""


def test_names_compatible_subset_not_disjoint():
    assert person_dedup._names_compatible("anna", "anna meyer")
    assert person_dedup._names_compatible("anna meyer", "anna meyer")
    assert not person_dedup._names_compatible("anna meyer", "anna schmidt")
    assert not person_dedup._names_compatible("", "anna")


# --- detection: precision ----------------------------------------------------


def test_shared_phone_and_compatible_name_is_a_candidate(conn):
    _person(conn, "a", "Anna Meyer", "mdopp", facts=[("phone", "0177 5524222")])
    _person(
        conn, "b", "Anna", "mdopp", source="caldav", facts=[("phone", "+49177 5524222")]
    )
    cands = person_dedup.find_merge_candidates(conn, "mdopp")
    assert len(cands) == 1
    assert {cands[0]["primary"], cands[0]["secondary"]} == {"a", "b"}
    assert cands[0]["reason"] == ["phone:491775524222"]


def test_shared_email_across_sources_is_a_candidate(conn):
    _person(conn, "a", "Michael Dopp", "mdopp", facts=[("email", "mdopp@web.de")])
    _person(
        conn,
        "b",
        "Michael Dopp",
        "mdopp",
        source="caldav",
        facts=[("email", "MDOPP@web.de")],
    )
    cands = person_dedup.find_merge_candidates(conn, "mdopp")
    assert len(cands) == 1


def test_no_false_merge_on_name_only(conn):
    # Two distinct "Anna Meyer"s with NO shared contact key must never be offered.
    _person(conn, "a", "Anna Meyer", "mdopp", facts=[("phone", "0177 1111111")])
    _person(conn, "b", "Anna Meyer", "mdopp", facts=[("phone", "0177 2222222")])
    assert person_dedup.find_merge_candidates(conn, "mdopp") == []


def test_no_false_merge_on_shared_key_but_disjoint_names(conn):
    # A shared phone but clearly different people (roommates share a landline) is
    # NOT a candidate — the name guard blocks it.
    _person(conn, "a", "Anna Schmidt", "mdopp", facts=[("phone", "030 1234567")])
    _person(conn, "b", "Bernd Müller", "mdopp", facts=[("phone", "030 1234567")])
    assert person_dedup.find_merge_candidates(conn, "mdopp") == []


def test_unnamed_contact_key_share_is_not_a_candidate(conn):
    # An email with no name signal on one side is not enough (needs both).
    _person(conn, "a", "", "mdopp", facts=[("email", "x@y.de")])
    _person(conn, "b", "", "mdopp", facts=[("email", "x@y.de")])
    assert person_dedup.find_merge_candidates(conn, "mdopp") == []


# --- detection: per-resident isolation ---------------------------------------


def test_no_cross_resident_candidate(conn):
    # Same name + same phone but owned by different residents: never offered.
    _person(conn, "a", "Anna Meyer", "mdopp", facts=[("phone", "0177 5524222")])
    _person(conn, "b", "Anna Meyer", "lena", facts=[("phone", "0177 5524222")])
    assert person_dedup.find_merge_candidates(conn, "mdopp") == []
    assert person_dedup.find_merge_candidates(conn, "lena") == []


def test_shared_household_person_is_in_scope(conn):
    _person(conn, "a", "Anna Meyer", "household", facts=[("phone", "0177 5524222")])
    _person(conn, "b", "Anna", "mdopp", facts=[("phone", "0177 5524222")])
    cands = person_dedup.find_merge_candidates(conn, "mdopp")
    assert len(cands) == 1


# --- preview -----------------------------------------------------------------


def test_preview_is_read_only_and_unions(conn):
    _person(
        conn,
        "a",
        "Anna Meyer",
        "mdopp",
        facts=[("phone", "0177 5524222")],
        aliases=["Anni"],
    )
    _person(
        conn,
        "b",
        "Anna",
        "mdopp",
        source="caldav",
        facts=[("email", "anna@x.de")],
    )
    before = conn.execute("SELECT COUNT(*) AS n FROM entities").fetchone()["n"]
    prev = person_dedup.preview_merge(conn, "a", "b", "mdopp")
    assert prev is not None
    assert "Anni" in prev["aliases"] and "Anna" in prev["aliases"]
    assert {"phone:491775524222", "email:anna@x.de"} <= set(prev["keys"])
    assert conn.execute("SELECT COUNT(*) AS n FROM entities").fetchone()["n"] == before


def test_preview_refuses_cross_resident(conn):
    _person(conn, "a", "Anna", "mdopp", facts=[("phone", "0177 5524222")])
    _person(conn, "b", "Anna", "lena", facts=[("phone", "0177 5524222")])
    assert person_dedup.preview_merge(conn, "a", "b", "mdopp") is None


# --- merge: confirmation-gated, owner-scoped ---------------------------------


def test_merge_moves_aliases_facts_events(conn):
    _person(
        conn,
        "a",
        "Anna Meyer",
        "mdopp",
        facts=[("phone", "0177 5524222")],
        aliases=["Anni"],
    )
    _person(
        conn,
        "b",
        "Anna",
        "mdopp",
        source="caldav",
        facts=[("email", "anna@x.de")],
    )
    conn.execute(
        "INSERT INTO events (id, ts, resident_uid, kind, source)"
        " VALUES ('e1', '2026-01-01', 'mdopp', 'meeting', 'caldav')"
    )
    conn.execute(
        "INSERT INTO event_entities (event_id, entity_id, role)"
        " VALUES ('e1', 'b', 'attendee')"
    )
    mid = person_dedup.merge_persons(
        conn, primary_id="a", secondary_id="b", uid="mdopp"
    )
    assert mid is not None
    # secondary gone, primary carries both sources' facts + the alias.
    assert conn.execute("SELECT 1 FROM entities WHERE id = 'b'").fetchone() is None
    preds = {
        r["predicate"]
        for r in conn.execute(
            "SELECT predicate FROM facts WHERE subject_entity_id = 'a'"
        ).fetchall()
    }
    assert preds == {"phone", "email"}
    aliases = {
        r["alias"]
        for r in conn.execute(
            "SELECT alias FROM entity_aliases WHERE entity_id = 'a'"
        ).fetchall()
    }
    assert {"Anni", "Anna Meyer", "Anna"} <= aliases
    # the event edge now points at the primary.
    assert (
        conn.execute(
            "SELECT entity_id FROM event_entities WHERE event_id = 'e1'"
        ).fetchone()["entity_id"]
        == "a"
    )


def test_merge_refuses_cross_resident(conn):
    _person(conn, "a", "Anna", "mdopp", facts=[("phone", "0177 5524222")])
    _person(conn, "b", "Anna", "lena", facts=[("phone", "0177 5524222")])
    assert (
        person_dedup.merge_persons(conn, primary_id="a", secondary_id="b", uid="mdopp")
        is None
    )
    # lena's person is untouched.
    assert conn.execute("SELECT 1 FROM entities WHERE id = 'b'").fetchone() is not None


def test_merge_refuses_self(conn):
    _person(conn, "a", "Anna", "mdopp", facts=[("phone", "0177 5524222")])
    assert (
        person_dedup.merge_persons(conn, primary_id="a", secondary_id="a", uid="mdopp")
        is None
    )


# --- provenance / undo -------------------------------------------------------


def test_merge_records_audit_trail(conn):
    _person(conn, "a", "Anna Meyer", "mdopp", facts=[("phone", "0177 5524222")])
    _person(conn, "b", "Anna", "mdopp", source="caldav", facts=[("email", "anna@x.de")])
    mid = person_dedup.merge_persons(
        conn, primary_id="a", secondary_id="b", uid="mdopp"
    )
    row = conn.execute("SELECT * FROM person_merges WHERE id = ?", (mid,)).fetchone()
    assert row["secondary_entity_id"] == "b"
    assert row["secondary_name"] == "Anna"
    assert row["secondary_source"] == "caldav"
    assert row["merged_by"] == "mdopp"
    assert row["undone_at"] is None


def test_undo_restores_the_secondary(conn):
    _person(
        conn,
        "a",
        "Anna Meyer",
        "mdopp",
        facts=[("phone", "0177 5524222")],
    )
    _person(
        conn,
        "b",
        "Anna",
        "mdopp",
        source="caldav",
        facts=[("email", "anna@x.de")],
        aliases=["Änni"],
    )
    mid = person_dedup.merge_persons(
        conn, primary_id="a", secondary_id="b", uid="mdopp"
    )
    assert conn.execute("SELECT 1 FROM entities WHERE id = 'b'").fetchone() is None

    assert person_dedup.undo_merge(conn, mid, "mdopp") is True
    b = conn.execute("SELECT * FROM entities WHERE id = 'b'").fetchone()
    assert b is not None
    assert b["canonical_name"] == "Anna"
    assert b["resident_uid"] == "mdopp"
    assert b["source"] == "caldav"
    # its own facts + aliases come back.
    assert (
        conn.execute(
            "SELECT value FROM facts WHERE subject_entity_id = 'b' AND predicate = 'email'"
        ).fetchone()["value"]
        == "anna@x.de"
    )
    b_aliases = {
        r["alias"]
        for r in conn.execute(
            "SELECT alias FROM entity_aliases WHERE entity_id = 'b'"
        ).fetchall()
    }
    assert {"Anna", "Änni"} <= b_aliases
    # the trail is marked undone → a second undo is a no-op.
    assert person_dedup.undo_merge(conn, mid, "mdopp") is False


def test_undo_refuses_cross_resident(conn):
    _person(conn, "a", "Anna Meyer", "household", facts=[("phone", "0177 5524222")])
    _person(
        conn, "b", "Anna", "household", source="caldav", facts=[("email", "a@x.de")]
    )
    mid = person_dedup.merge_persons(
        conn, primary_id="a", secondary_id="b", uid="household"
    )
    # a different resident can't undo a merge of a person outside their scope.
    conn.execute(
        "UPDATE person_merges SET secondary_resident_uid = 'lena' WHERE id = ?",
        (mid,),
    )
    assert person_dedup.undo_merge(conn, mid, "mdopp") is False

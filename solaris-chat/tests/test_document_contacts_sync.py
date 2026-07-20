"""Provider organizations → Radicale address books as vCards (#doc-graph).

`document_contacts_sync.sync_contacts` reads `organization` entities + their
contact facts and drops one `<uid>.vcf` per org into the owner's Radicale
`contacts/` collection (filesystem write, like the Google-import). These tests
prove the write target, the vCard content, the stable overwrite UID, the
household skip, and the disabled no-op.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from solaris_chat.engine import document_contacts_sync

_SCHEMA = """
CREATE TABLE entities (
  id TEXT PRIMARY KEY, type TEXT NOT NULL, canonical_name TEXT NOT NULL,
  resident_uid TEXT NOT NULL, source TEXT NOT NULL, content_hash TEXT NOT NULL,
  updated TEXT NOT NULL DEFAULT (datetime('now')));
CREATE TABLE facts (
  id TEXT PRIMARY KEY, subject_entity_id TEXT, resident_uid TEXT NOT NULL,
  predicate TEXT NOT NULL, value TEXT NOT NULL, confidence REAL,
  source TEXT NOT NULL, timestamp TEXT NOT NULL DEFAULT (datetime('now')));
"""


def _org(conn, eid, name, resident, facts):
    conn.execute(
        "INSERT INTO entities (id, type, canonical_name, resident_uid, source,"
        " content_hash) VALUES (?, 'organization', ?, ?, 'documents', 'h')",
        (eid, name, resident),
    )
    for i, (pred, val, conf) in enumerate(facts):
        conn.execute(
            "INSERT INTO facts (id, subject_entity_id, resident_uid, predicate,"
            " value, confidence, source) VALUES (?, ?, ?, ?, ?, ?, 'documents')",
            (f"{eid}-{i}", eid, resident, pred, val, conf),
        )


def _db(tmp_path):
    db = str(tmp_path / "solaris.db")
    conn = sqlite3.connect(db)
    conn.executescript(_SCHEMA)
    _org(
        conn,
        "o-ergo",
        "ERGO Versicherung AG",
        "mdopp",
        [
            ("phone", "05404 5209", 0.6),
            ("email", "dirk.mutert@ergo.de", 0.6),
            ("contact_person", "Dirk Mutert", 0.6),
        ],
    )
    conn.commit()
    conn.close()
    return db


def _cards(radicale_data, user):
    coll = Path(radicale_data) / "collections" / "collection-root" / user / "contacts"
    return sorted(coll.glob("*.vcf")) if coll.exists() else []


def test_sync_writes_vcard_to_owner_addressbook(tmp_path):
    db = _db(tmp_path)
    rad = str(tmp_path / "radicale")
    out = document_contacts_sync.sync_contacts(db, rad)
    assert out == {"written": 1, "skipped": 0}
    cards = _cards(rad, "mdopp")
    assert len(cards) == 1
    text = cards[0].read_text()
    assert "FN:ERGO Versicherung AG" in text
    assert "05404 5209" in text
    assert "dirk.mutert@ergo.de" in text
    assert "Dirk Mutert" in text  # in the NOTE
    # A Solaris-owned UID so a re-sync overwrites and human contacts are untouched.
    assert "UID:solaris-provider-o-ergo" in text


def test_sync_is_idempotent_overwrite(tmp_path):
    db = _db(tmp_path)
    rad = str(tmp_path / "radicale")
    document_contacts_sync.sync_contacts(db, rad)
    document_contacts_sync.sync_contacts(db, rad)
    # Same stable UID → one file, not two.
    assert len(_cards(rad, "mdopp")) == 1


def test_household_org_is_skipped(tmp_path):
    db = str(tmp_path / "solaris.db")
    conn = sqlite3.connect(db)
    conn.executescript(_SCHEMA)
    _org(conn, "o-shared", "Stadtwerke", "household", [("phone", "0", 0.6)])
    conn.commit()
    conn.close()
    out = document_contacts_sync.sync_contacts(db, str(tmp_path / "radicale"))
    assert out == {"written": 0, "skipped": 1}


def test_disabled_when_no_radicale_data(tmp_path):
    db = _db(tmp_path)
    assert document_contacts_sync.sync_contacts(db, "") == {"written": 0, "skipped": 0}

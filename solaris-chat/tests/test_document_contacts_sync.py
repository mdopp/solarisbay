"""Provider organizations → the Solaris address book as vCards (#doc-graph).

`document_contacts_sync.sync_contacts` reads `organization` entities + their
contact facts and PUTs one vCard per org into the dedicated `solaris` account's
CardDAV collection (authenticated HTTP, not a filesystem mount). These tests
mock the DAV `put_item` and prove the vCard content, the stable overwrite UID,
the target collection/suffix, and the disabled no-op.
"""

from __future__ import annotations

import sqlite3

from solaris_chat.engine import document_contacts_sync
from solaris_chat.engine.document_contacts_sync import sync_contacts

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

_URL = "https://caldav.example/solaris/anbieter/"


def _org(conn, eid, name, facts):
    conn.execute(
        "INSERT INTO entities (id, type, canonical_name, resident_uid, source,"
        " content_hash) VALUES (?, 'organization', ?, 'mdopp', 'documents', 'h')",
        (eid, name),
    )
    for i, (pred, val) in enumerate(facts):
        conn.execute(
            "INSERT INTO facts (id, subject_entity_id, resident_uid, predicate,"
            " value, confidence, source) VALUES (?, ?, 'mdopp', ?, ?, 0.6, 'd')",
            (f"{eid}-{i}", eid, pred, val),
        )


def _db(tmp_path):
    db = str(tmp_path / "solaris.db")
    conn = sqlite3.connect(db)
    conn.executescript(_SCHEMA)
    _org(
        conn,
        "o-ergo",
        "ERGO Versicherung AG",
        [
            ("phone", "05404 5209"),
            ("email", "dirk.mutert@ergo.de"),
            ("contact_person", "Dirk Mutert"),
        ],
    )
    conn.commit()
    conn.close()
    return db


def _capture(monkeypatch):
    calls = []

    async def fake_put(self, collection_url, uid, body, *, suffix, content_type):
        calls.append(
            {
                "url": collection_url,
                "uid": uid,
                "body": body,
                "suffix": suffix,
                "content_type": content_type,
            }
        )
        return collection_url + uid

    monkeypatch.setattr(document_contacts_sync.HttpDavClient, "put_item", fake_put)
    return calls


async def test_sync_puts_vcard_to_collection(tmp_path, monkeypatch):
    calls = _capture(monkeypatch)
    out = await sync_contacts(_db(tmp_path), _URL, "solaris", "pw")
    assert out == {"written": 1, "failed": 0}
    assert len(calls) == 1
    c = calls[0]
    assert c["url"] == _URL and c["suffix"] == ".vcf"
    assert c["uid"] == "solaris-provider-o-ergo"  # stable overwrite UID
    assert "FN:ERGO Versicherung AG" in c["body"]
    assert "05404 5209" in c["body"] and "dirk.mutert@ergo.de" in c["body"]
    assert "Dirk Mutert" in c["body"]  # in the NOTE


async def test_one_bad_card_does_not_abort(tmp_path, monkeypatch):
    db = _db(tmp_path)
    conn = sqlite3.connect(db)
    _org(conn, "o-lbs", "LBS", [("phone", "1")])
    conn.commit()
    conn.close()

    async def flaky_put(self, collection_url, uid, body, *, suffix, content_type):
        if "o-ergo" in uid:
            raise RuntimeError("boom")

    monkeypatch.setattr(document_contacts_sync.HttpDavClient, "put_item", flaky_put)
    out = await sync_contacts(db, _URL, "solaris", "pw")
    assert out == {"written": 1, "failed": 1}


async def test_disabled_when_unconfigured(tmp_path, monkeypatch):
    calls = _capture(monkeypatch)
    db = _db(tmp_path)
    assert await sync_contacts(db, "", "solaris", "pw") == {"written": 0, "failed": 0}
    assert await sync_contacts(db, _URL, "", "pw") == {"written": 0, "failed": 0}
    assert await sync_contacts(db, _URL, "solaris", "") == {"written": 0, "failed": 0}
    assert calls == []

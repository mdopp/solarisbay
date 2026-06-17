"""Concept/entity page — aggregator + view contract (#502 phase 1).

Covers the `/api/concept/<id>` aggregator (entity resolution + OKF
description/facts/events + source docs + chat/note backlinks + live HA card),
the `/c/<id>` SPA shell deep-link, and the supporting read functions + the
static view contract the client router depends on.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from solaris_chat import mentions_store, notes_search
from solaris_chat.engine.knowledge import okf, projection
from solaris_chat.engine.tools import ha
from solaris_chat.server import STATIC_DIR, build_app

# Migration 0016 (entities/facts/events/concepts) + 0006 (mentions), replayed
# locally so the aggregator runs against a real sqlite db without alembic.
_SCHEMA = """
CREATE TABLE entities (
  id TEXT PRIMARY KEY, type TEXT NOT NULL, canonical_name TEXT NOT NULL,
  resident_uid TEXT NOT NULL, source TEXT NOT NULL, content_hash TEXT NOT NULL,
  updated TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE entity_aliases (
  entity_id TEXT NOT NULL, alias TEXT NOT NULL, PRIMARY KEY (entity_id, alias)
);
CREATE TABLE facts (
  id TEXT PRIMARY KEY, subject_entity_id TEXT, resident_uid TEXT NOT NULL,
  predicate TEXT NOT NULL, value TEXT NOT NULL, confidence REAL,
  source TEXT NOT NULL, timestamp TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE events (
  id TEXT PRIMARY KEY, ts TEXT NOT NULL, resident_uid TEXT NOT NULL,
  kind TEXT NOT NULL, source TEXT NOT NULL
);
CREATE TABLE event_entities (
  event_id TEXT NOT NULL, entity_id TEXT NOT NULL, role TEXT NOT NULL,
  PRIMARY KEY (event_id, entity_id, role)
);
CREATE TABLE concepts (
  id TEXT PRIMARY KEY, ref_id TEXT NOT NULL, ref_kind TEXT NOT NULL,
  okf_path TEXT NOT NULL, embedding_id TEXT, content_hash TEXT NOT NULL,
  updated TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE mentions (
  session_id TEXT NOT NULL, message_ref INTEGER NOT NULL, kind TEXT NOT NULL,
  value TEXT NOT NULL, owner_uid TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (session_id, message_ref, kind, value)
);
"""


def _db(tmp_path) -> str:
    path = str(tmp_path / "solaris.db")
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA)
    # An entity (person "Anna") with an alias, a fact, and an event.
    conn.execute(
        "INSERT INTO entities (id, type, canonical_name, resident_uid, source,"
        " content_hash) VALUES (?, ?, ?, ?, ?, ?)",
        ("ent-anna", "person", "Anna", "mdopp", "contacts:1", "h"),
    )
    conn.execute(
        "INSERT INTO entity_aliases (entity_id, alias) VALUES (?, ?)",
        ("ent-anna", "Anni"),
    )
    conn.execute(
        "INSERT INTO facts (id, subject_entity_id, resident_uid, predicate, value,"
        " source) VALUES (?, ?, ?, ?, ?, ?)",
        ("f1", "ent-anna", "mdopp", "rolle", "Schwester", "contacts:1"),
    )
    conn.execute(
        "INSERT INTO events (id, ts, resident_uid, kind, source)"
        " VALUES (?, ?, ?, ?, ?)",
        ("ev1", "2026-05-01T10:00", "mdopp", "birthday", "cal:1"),
    )
    conn.execute(
        "INSERT INTO event_entities (event_id, entity_id, role) VALUES (?, ?, ?)",
        ("ev1", "ent-anna", "celebrant"),
    )
    conn.execute(
        "INSERT INTO concepts (id, ref_id, ref_kind, okf_path, content_hash)"
        " VALUES (?, ?, ?, ?, ?)",
        ("c1", "ent-anna", "entity", "okf/people/anna.md", "h"),
    )
    # A different resident's "Anna" must not leak.
    conn.execute(
        "INSERT INTO entities (id, type, canonical_name, resident_uid, source,"
        " content_hash) VALUES (?, ?, ?, ?, ?, ?)",
        ("ent-anna-lena", "person", "Anna", "lena", "contacts:9", "h"),
    )
    # A chat-turn backlink for the owner.
    conn.execute(
        "INSERT INTO mentions (session_id, message_ref, kind, value, owner_uid)"
        " VALUES (?, ?, ?, ?, ?)",
        ("sess-7", 3, "person", "Anna", "mdopp"),
    )
    conn.commit()
    conn.close()
    return path


def _notes(tmp_path) -> str:
    root = tmp_path / "notes"
    (root / "okf" / "people").mkdir(parents=True)
    (root / "okf" / "people" / "anna.md").write_text(
        "---\ntype: person\nid: ent-anna\ndescription: Annas Konzept\n---\n\n"
        "Anna mag Tee.\n\n## Relationships\n\n- sister → [[people/mdopp]]\n",
        encoding="utf-8",
    )
    (root / "tagebuch.md").write_text(
        "# Tagebuch\nadded_by: mdopp\n\nHeute war Anna da.\n", encoding="utf-8"
    )
    return str(root)


# ---- read functions ----------------------------------------------------------


def test_read_concept_parses_description_and_drops_relationships():
    parsed = okf.read_concept(
        "---\ntype: person\ndescription: D\n---\n\nBody line.\n\n"
        "## Relationships\n\n- x → [[y]]\n"
    )
    assert parsed["description"] == "D"
    assert parsed["body"] == "Body line."


def test_resolve_entity_by_id_name_and_alias(tmp_path):
    conn = projection.open_conn(_db(tmp_path))
    try:
        assert projection.resolve_entity_id(conn, "ent-anna", "mdopp") == "ent-anna"
        assert projection.resolve_entity_id(conn, "Anna", "mdopp") == "ent-anna"
        assert projection.resolve_entity_id(conn, "Anni", "mdopp") == "ent-anna"
        assert projection.resolve_entity_id(conn, "nobody", "mdopp") is None
    finally:
        conn.close()


def test_resolve_entity_is_per_resident(tmp_path):
    conn = projection.open_conn(_db(tmp_path))
    try:
        # lena's "Anna" is a different entity; mdopp never resolves to it.
        assert projection.resolve_entity_id(conn, "Anna", "lena") == "ent-anna-lena"
        assert projection.resolve_entity_id(conn, "Anna", "mdopp") == "ent-anna"
    finally:
        conn.close()


def test_facts_and_events_for_entity(tmp_path):
    conn = projection.open_conn(_db(tmp_path))
    try:
        facts = projection.entity_facts(conn, "ent-anna")
        assert facts == [
            {"predicate": "rolle", "value": "Schwester", "confidence": None}
        ]
        events = projection.entity_events(conn, "ent-anna")
        assert events[0]["kind"] == "birthday"
        assert events[0]["role"] == "celebrant"
    finally:
        conn.close()


def test_mentions_backlinks_for(tmp_path):
    db = _db(tmp_path)
    links = mentions_store.backlinks_for(db, "mdopp", ["Anna", "Anni"])
    assert links == [{"session_id": "sess-7", "message_ref": 3, "value": "Anna"}]
    # Another resident sees none of mdopp's mentions.
    assert mentions_store.backlinks_for(db, "lena", ["Anna"]) == []


def test_notes_mentioning_excludes_okf_subtree(tmp_path):
    notes_dir = _notes(tmp_path)
    found = notes_search.notes_mentioning(notes_dir, ["Anna"], "mdopp")
    paths = [n["path"] for n in found]
    assert "tagebuch.md" in paths
    # The OKF concept file is surfaced as the source doc, not as a note backlink.
    assert all("okf" not in p for p in paths)


# ---- HA card-spec reuse ------------------------------------------------------


def test_card_spec_builds_from_live_state():
    spec = ha.card_spec(
        "sensor.kitchen",
        "21.5",
        {"friendly_name": "Küche", "unit_of_measurement": "°C"},
    )
    assert spec == {
        "entity_id": "sensor.kitchen",
        "name": "Küche",
        "domain": "sensor",
        "device_class": None,
        "state": "21.5",
        "unit": "°C",
    }


def test_card_spec_none_for_uncarded_domain():
    assert ha.card_spec("person.someone", "home", {}) is None


# ---- endpoint contract -------------------------------------------------------


async def test_concept_api_aggregates_entity(aiohttp_client, tmp_path):
    app = build_app(
        hermes=object(),
        remote_user_header="Remote-User",
        default_uid="household",
        solaris_db_path=_db(tmp_path),
        notes_dir=_notes(tmp_path),
    )
    client = await aiohttp_client(app)
    resp = await client.get("/api/concept/ent-anna", headers={"Remote-User": "mdopp"})
    assert resp.status == 200
    c = (await resp.json())["concept"]
    assert c["title"] == "Anna"
    assert c["type"] == "person"
    assert c["description"] == "Annas Konzept"
    assert "Anna mag Tee." in c["body"]
    assert {"predicate": "rolle", "value": "Schwester", "confidence": None} in c[
        "facts"
    ]
    assert c["events"][0]["kind"] == "birthday"
    okf_docs = [d for d in c["source_docs"] if d["kind"] == "okf"]
    note_docs = [d for d in c["source_docs"] if d["kind"] == "note"]
    assert okf_docs[0]["path"] == "okf/people/anna.md"
    assert any(d["path"] == "tagebuch.md" for d in note_docs)
    assert c["backlinks"] == [
        {"session_id": "sess-7", "message_ref": 3, "value": "Anna"}
    ]
    # No HA configured -> no live card, page still renders.
    assert c["ha_card"] is None


async def test_concept_api_unknown_id_degrades(aiohttp_client, tmp_path):
    app = build_app(
        hermes=object(),
        remote_user_header="Remote-User",
        default_uid="household",
        solaris_db_path=_db(tmp_path),
        notes_dir=_notes(tmp_path),
    )
    client = await aiohttp_client(app)
    resp = await client.get("/api/concept/nope", headers={"Remote-User": "mdopp"})
    assert resp.status == 200
    c = (await resp.json())["concept"]
    assert c["id"] == "nope"
    assert c["facts"] == [] and c["events"] == [] and c["backlinks"] == []


async def test_concept_api_adds_live_ha_card(aiohttp_client, tmp_path, monkeypatch):
    async def _fake_fetch(url, token, entity_id):
        return ha.card_spec(entity_id, "on", {"friendly_name": "Sofalicht"})

    monkeypatch.setattr("solaris_chat.server.fetch_card", _fake_fetch)
    app = build_app(
        hermes=object(),
        remote_user_header="Remote-User",
        default_uid="household",
        solaris_db_path=_db(tmp_path),
        notes_dir=_notes(tmp_path),
        hass_url="http://ha",
        hass_token="t",
    )
    client = await aiohttp_client(app)
    resp = await client.get(
        "/api/concept/light.sofalicht", headers={"Remote-User": "mdopp"}
    )
    assert resp.status == 200
    c = (await resp.json())["concept"]
    assert c["ha_card"]["domain"] == "light"
    assert c["ha_card"]["state"] == "on"
    assert c["title"] == "Sofalicht"


async def test_concept_shell_serves_spa(aiohttp_client, tmp_path):
    app = build_app(
        hermes=object(),
        remote_user_header="Remote-User",
        default_uid="household",
        solaris_db_path=_db(tmp_path),
        notes_dir=_notes(tmp_path),
    )
    client = await aiohttp_client(app)
    resp = await client.get("/c/ent-anna")
    assert resp.status == 200
    assert resp.headers.get("Content-Type", "").startswith("text/html")


# ---- static view contract the client router depends on -----------------------


@pytest.mark.parametrize(
    "sentinel",
    [
        "function openConcept(",
        "function renderConceptCard(",
        "function routeFromLocation(",
        "#\\/c\\/",  # the hash-route pattern
        '"/api/concept/"',  # the aggregator the page fetches
        "renderHaCard(c.ha_card",  # reuses the chat's HA card renderer
    ],
)
def test_index_html_concept_view_contract(sentinel):
    html = (Path(STATIC_DIR) / "index.html").read_text(encoding="utf-8")
    assert sentinel in html

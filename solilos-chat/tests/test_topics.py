"""Tests for the topics store + the topic endpoints (#241)."""

from __future__ import annotations

import sqlite3

import pytest

from solilos_chat import topics_store
from solilos_chat.server import build_app

# The schema the 0004/0005 migrations create, replayed locally so the store and
# endpoint tests run against a real sqlite db without alembic.
_SCHEMA = """
CREATE TABLE topics (
  slug TEXT PRIMARY KEY,
  display_name TEXT NOT NULL,
  parent TEXT,
  scope TEXT NOT NULL DEFAULT 'resident',
  owner_uid TEXT,
  default_model TEXT,
  default_persona TEXT,
  color TEXT,
  archived INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE session_topics (
  session_id TEXT NOT NULL,
  topic_slug TEXT NOT NULL REFERENCES topics(slug),
  role TEXT NOT NULL DEFAULT 'secondary',
  owner_uid TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (session_id, topic_slug)
);
CREATE UNIQUE INDEX session_topics_one_primary_idx
  ON session_topics (session_id) WHERE role = 'primary';
CREATE INDEX session_topics_session_idx ON session_topics (session_id);
"""


def _db(tmp_path) -> str:
    path = str(tmp_path / "solilos.db")
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA)
    conn.executemany(
        "INSERT INTO topics (slug, display_name, scope, owner_uid, color) "
        "VALUES (?, ?, ?, ?, ?)",
        [
            ("household", "Household", "shared", None, "#0052CC"),
            ("projekt/wintergarten", "Wintergarten", "resident", "mdopp", "#22aa55"),
            ("lenas-topic", "Lena", "resident", "lena", None),
        ],
    )
    conn.commit()
    conn.close()
    return path


def test_list_topics_scopes_to_resident(tmp_path):
    db = _db(tmp_path)
    slugs = {t["slug"] for t in topics_store.list_topics(db, "mdopp")}
    # mdopp sees shared (household) + own (wintergarten), not lena's resident one.
    assert slugs == {"household", "projekt/wintergarten"}


def test_list_topics_missing_db_is_empty(tmp_path):
    assert topics_store.list_topics(str(tmp_path / "nope.db"), "mdopp") == []


def test_set_primary_replaces_existing(tmp_path):
    db = _db(tmp_path)
    topics_store.set_primary(db, "sess-1", "household", "mdopp")
    topics_store.set_primary(db, "sess-1", "projekt/wintergarten", "mdopp")
    got = topics_store.get_session_topics(db, "sess-1", "mdopp")
    assert got == {"primary": "projekt/wintergarten", "secondary": []}


def test_only_one_primary_row_per_session(tmp_path):
    db = _db(tmp_path)
    topics_store.set_primary(db, "sess-1", "household", "mdopp")
    topics_store.set_primary(db, "sess-1", "projekt/wintergarten", "mdopp")
    conn = sqlite3.connect(db)
    n = conn.execute(
        "SELECT COUNT(*) FROM session_topics WHERE session_id='sess-1' AND role='primary'"
    ).fetchone()[0]
    conn.close()
    assert n == 1


def test_secondary_promote_to_primary_no_collision(tmp_path):
    db = _db(tmp_path)
    topics_store.add_secondary(db, "sess-1", "household", "mdopp")
    # Promoting a slug that is already a secondary tag must not hit the PK.
    topics_store.set_primary(db, "sess-1", "household", "mdopp")
    got = topics_store.get_session_topics(db, "sess-1", "mdopp")
    assert got == {"primary": "household", "secondary": []}


def test_add_and_remove_secondary(tmp_path):
    db = _db(tmp_path)
    topics_store.add_secondary(db, "sess-1", "household", "mdopp")
    topics_store.add_secondary(db, "sess-1", "household", "mdopp")  # idempotent
    topics_store.add_secondary(db, "sess-1", "projekt/wintergarten", "mdopp")
    got = topics_store.get_session_topics(db, "sess-1", "mdopp")
    assert got["primary"] is None
    assert got["secondary"] == ["household", "projekt/wintergarten"]
    topics_store.remove_topic(db, "sess-1", "household", "mdopp")
    assert topics_store.get_session_topics(db, "sess-1", "mdopp")["secondary"] == [
        "projekt/wintergarten"
    ]


def test_assignments_scoped_per_resident(tmp_path):
    db = _db(tmp_path)
    topics_store.set_primary(db, "sess-1", "household", "mdopp")
    # lena never sees mdopp's assignment on the same session id (D3).
    assert topics_store.get_session_topics(db, "sess-1", "lena") == {
        "primary": None,
        "secondary": [],
    }


def test_primary_topics_for_list(tmp_path):
    db = _db(tmp_path)
    topics_store.set_primary(db, "sess-1", "household", "mdopp")
    topics_store.add_secondary(db, "sess-2", "household", "mdopp")
    got = topics_store.primary_topics_for(db, ["sess-1", "sess-2", "sess-3"], "mdopp")
    assert got == {"sess-1": "household"}


def _seed_topic_defaults(db: str, slug: str, model, persona) -> None:
    conn = sqlite3.connect(db)
    conn.execute(
        "UPDATE topics SET default_model = ?, default_persona = ? WHERE slug = ?",
        (model, persona, slug),
    )
    conn.commit()
    conn.close()


def test_topic_defaults_reads_model_and_persona(tmp_path):
    db = _db(tmp_path)
    _seed_topic_defaults(db, "projekt/wintergarten", "gemma4:12b", "technical")
    assert topics_store.topic_defaults(db, "projekt/wintergarten") == {
        "default_model": "gemma4:12b",
        "default_persona": "technical",
    }


def test_topic_defaults_null_when_unset(tmp_path):
    db = _db(tmp_path)
    # The seeded household row has no default_model/persona (both NULL).
    assert topics_store.topic_defaults(db, "household") == {
        "default_model": None,
        "default_persona": None,
    }


def test_topic_defaults_missing_db_or_row(tmp_path):
    db = _db(tmp_path)
    assert topics_store.topic_defaults(db, "nope") == {
        "default_model": None,
        "default_persona": None,
    }
    assert topics_store.topic_defaults(str(tmp_path / "no.db"), "household") == {
        "default_model": None,
        "default_persona": None,
    }


# ---- Endpoint tests ----

from tests.test_server import _FakeHermes  # noqa: E402


async def test_topics_endpoint_lists_registry(aiohttp_client, tmp_path):
    db = _db(tmp_path)
    app = build_app(
        hermes=_FakeHermes(),
        remote_user_header="Remote-User",
        default_uid="household",
        solilos_db_path=db,
    )
    client = await aiohttp_client(app)
    resp = await client.get("/api/topics", headers={"Remote-User": "mdopp"})
    body = await resp.json()
    assert resp.status == 200
    slugs = {t["slug"] for t in body["topics"]}
    assert slugs == {"household", "projekt/wintergarten"}


async def test_session_topics_set_and_read(aiohttp_client, tmp_path):
    db = _db(tmp_path)
    app = build_app(
        hermes=_FakeHermes(),
        remote_user_header="Remote-User",
        default_uid="household",
        solilos_db_path=db,
    )
    client = await aiohttp_client(app)
    hdr = {"Remote-User": "mdopp"}
    resp = await client.post(
        "/api/sessions/sess-1/topics",
        json={"action": "primary", "slug": "household"},
        headers=hdr,
    )
    body = await resp.json()
    assert resp.status == 200
    assert body["primary"] == "household"
    resp = await client.post(
        "/api/sessions/sess-1/topics",
        json={"action": "add_secondary", "slug": "projekt/wintergarten"},
        headers=hdr,
    )
    body = await resp.json()
    assert body["secondary"] == ["projekt/wintergarten"]
    resp = await client.get("/api/sessions/sess-1/topics", headers=hdr)
    body = await resp.json()
    assert body["primary"] == "household"
    assert body["secondary"] == ["projekt/wintergarten"]


async def test_session_topics_invalid_action_rejected(aiohttp_client, tmp_path):
    db = _db(tmp_path)
    app = build_app(
        hermes=_FakeHermes(),
        remote_user_header="Remote-User",
        default_uid="household",
        solilos_db_path=db,
    )
    client = await aiohttp_client(app)
    resp = await client.post(
        "/api/sessions/sess-1/topics",
        json={"action": "bogus", "slug": "household"},
        headers={"Remote-User": "mdopp"},
    )
    assert resp.status == 400


@pytest.mark.parametrize("payload", [{"slug": ""}, {"action": "primary"}])
async def test_session_topics_empty_slug_rejected(aiohttp_client, tmp_path, payload):
    db = _db(tmp_path)
    app = build_app(
        hermes=_FakeHermes(),
        remote_user_header="Remote-User",
        default_uid="household",
        solilos_db_path=db,
    )
    client = await aiohttp_client(app)
    resp = await client.post(
        "/api/sessions/sess-1/topics",
        json=payload,
        headers={"Remote-User": "mdopp"},
    )
    assert resp.status == 400


async def test_topic_binds_model_persona_and_persists_primary(aiohttp_client, tmp_path):
    # A new chat started under a topic adopts the topic's default model +
    # persona at session create (D2/#242): default_model wins over the FAST
    # routing model, default_persona over the body's overlay; the topic is
    # persisted as the session's primary assignment.
    db = _db(tmp_path)
    _seed_topic_defaults(db, "projekt/wintergarten", "gemma4:12b", "technical")
    fake = _FakeHermes()
    app = build_app(
        hermes=fake,
        remote_user_header="Remote-User",
        default_uid="household",
        solilos_db_path=db,
        fast_model="gemma4:e2b",
        thorough_model="gemma4:12b",
    )
    client = await aiohttp_client(app)
    resp = await client.post(
        "/api/chat",
        json={
            "input": "welche Lichter sind an",  # a FAST turn → would route to e2b
            "personality": "concise",  # would set the concise overlay
            "topic": "projekt/wintergarten",
        },
        headers={"Remote-User": "mdopp"},
    )
    assert resp.status == 200
    from solilos_chat import personalities

    assert fake.models == ["gemma4:12b"]
    assert fake.created_prompts == [personalities.system_prompt_for("technical")]
    sid = (await resp.json())["session_id"]
    assigned = topics_store.get_session_topics(db, sid, "mdopp")
    assert assigned["primary"] == "projekt/wintergarten"


async def test_topic_without_defaults_falls_back(aiohttp_client, tmp_path):
    # A topic with no default_model/persona (household seed) → normal routing
    # model + the body's personality overlay (independent fallbacks).
    db = _db(tmp_path)
    fake = _FakeHermes()
    app = build_app(
        hermes=fake,
        remote_user_header="Remote-User",
        default_uid="household",
        solilos_db_path=db,
        fast_model="gemma4:e2b",
        thorough_model="gemma4:12b",
    )
    client = await aiohttp_client(app)
    resp = await client.post(
        "/api/chat",
        json={
            "input": "welche Lichter sind an",
            "personality": "concise",
            "topic": "household",
        },
        headers={"Remote-User": "mdopp"},
    )
    assert resp.status == 200
    from solilos_chat import personalities

    assert fake.models == ["gemma4:e2b"]
    assert fake.created_prompts == [personalities.system_prompt_for("concise")]


async def test_no_topic_uses_routing_default(aiohttp_client, tmp_path):
    db = _db(tmp_path)
    fake = _FakeHermes()
    app = build_app(
        hermes=fake,
        remote_user_header="Remote-User",
        default_uid="household",
        solilos_db_path=db,
        fast_model="gemma4:e2b",
        thorough_model="gemma4:12b",
    )
    client = await aiohttp_client(app)
    resp = await client.post(
        "/api/chat",
        json={"input": "welche Lichter sind an"},
        headers={"Remote-User": "mdopp"},
    )
    assert resp.status == 200
    assert fake.models == ["gemma4:e2b"]


async def test_changing_topic_mid_session_does_not_rebind(aiohttp_client, tmp_path):
    # The bound model/persona are fixed at create (Hermes binds them only then).
    # Re-assigning the primary topic on an EXISTING session updates the label +
    # future ingestion tag but cannot retroactively change the live session's
    # model/persona — the limitation #242 documents.
    db = _db(tmp_path)
    _seed_topic_defaults(db, "projekt/wintergarten", "gemma4:12b", "technical")
    fake = _FakeHermes()
    app = build_app(
        hermes=fake,
        remote_user_header="Remote-User",
        default_uid="household",
        solilos_db_path=db,
        fast_model="gemma4:e2b",
        thorough_model="gemma4:12b",
    )
    client = await aiohttp_client(app)
    hdr = {"Remote-User": "mdopp"}
    # First turn creates the session under no topic → fast model.
    resp = await client.post(
        "/api/chat", json={"input": "welche Lichter sind an"}, headers=hdr
    )
    sid = (await resp.json())["session_id"]
    assert fake.models == ["gemma4:e2b"]
    # Now assign a topic with a 12b default to the existing session.
    await client.post(
        f"/api/sessions/{sid}/topics",
        json={"action": "primary", "slug": "projekt/wintergarten"},
        headers=hdr,
    )
    # A follow-up turn reuses the SAME session: no new create_session, so the
    # model stays e2b — the topic default does not retroactively rebind it.
    resp = await client.post(
        "/api/chat", json={"input": "und jetzt", "session_id": sid}, headers=hdr
    )
    assert resp.status == 200
    assert fake.models == ["gemma4:e2b"]  # unchanged — still one create, e2b


def test_topic_context_hint_for_primary(tmp_path):
    db = _db(tmp_path)
    topics_store.set_primary(db, "sess-1", "projekt/wintergarten", "mdopp")
    hint = topics_store.topic_context_hint(db, "sess-1", "mdopp")
    # Machine-readable #topic/<slug> token (hierarchical slug) + display name.
    assert hint == "[Active topic: Wintergarten #topic/projekt/wintergarten]"


def test_topic_context_hint_none_without_topic(tmp_path):
    db = _db(tmp_path)
    assert topics_store.topic_context_hint(db, "sess-1", "mdopp") is None


def test_topic_context_hint_scoped_per_resident(tmp_path):
    db = _db(tmp_path)
    topics_store.set_primary(db, "sess-1", "household", "mdopp")
    # lena does not see mdopp's assignment → no hint on the same session id.
    assert topics_store.topic_context_hint(db, "sess-1", "lena") is None


def test_topic_context_hint_missing_db(tmp_path):
    assert (
        topics_store.topic_context_hint(str(tmp_path / "no.db"), "s", "mdopp") is None
    )


async def test_turn_carries_topic_hint_when_topic_active(aiohttp_client, tmp_path):
    # A turn in a topic chat gets the #topic/<slug> context line prepended so an
    # ingestion skill in the turn knows which topic to stamp (#243).
    db = _db(tmp_path)
    fake = _FakeHermes()
    app = build_app(
        hermes=fake,
        remote_user_header="Remote-User",
        default_uid="household",
        solilos_db_path=db,
    )
    client = await aiohttp_client(app)
    resp = await client.post(
        "/api/chat",
        json={"input": "merk dir das", "topic": "projekt/wintergarten"},
        headers={"Remote-User": "mdopp"},
    )
    assert resp.status == 200
    sent = fake.turns[-1][1]
    assert sent.startswith("[Active topic: Wintergarten #topic/projekt/wintergarten]")
    assert sent.endswith("merk dir das")


async def test_turn_has_no_hint_without_topic(aiohttp_client, tmp_path):
    db = _db(tmp_path)
    fake = _FakeHermes()
    app = build_app(
        hermes=fake,
        remote_user_header="Remote-User",
        default_uid="household",
        solilos_db_path=db,
    )
    client = await aiohttp_client(app)
    resp = await client.post(
        "/api/chat",
        json={"input": "merk dir das"},
        headers={"Remote-User": "mdopp"},
    )
    assert resp.status == 200
    # No active topic → the turn text is the user input verbatim, no hint.
    assert fake.turns[-1][1] == "merk dir das"


async def test_session_list_annotates_primary_topic(aiohttp_client, tmp_path):
    from solilos_chat import marker

    db = _db(tmp_path)
    store = [
        {"id": "sess-1", "title": marker.marker_for("mdopp") + "A chat"},
        {"id": "sess-2", "title": marker.marker_for("mdopp") + "Another"},
    ]
    topics_store.set_primary(db, "sess-1", "household", "mdopp")
    app = build_app(
        hermes=_FakeHermes(store=store),
        remote_user_header="Remote-User",
        default_uid="household",
        solilos_db_path=db,
    )
    client = await aiohttp_client(app)
    resp = await client.get("/api/sessions", headers={"Remote-User": "mdopp"})
    body = await resp.json()
    by_id = {s["id"]: s for s in body["sessions"]}
    assert by_id["sess-1"]["primary_topic"] == "household"
    assert by_id["sess-2"]["primary_topic"] is None

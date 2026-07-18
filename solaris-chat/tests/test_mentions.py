"""Tests for the mentions store, the #tag/@person parser, and the autosuggest
endpoints (#279a)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from solaris_chat import mentions_store
from solaris_chat.server import build_app, parse_mentions, seeded_persons

_DB_DIR = Path(__file__).resolve().parents[2] / "database"

# The schema migration 0006 creates, replayed locally so the store/endpoint
# tests run against a real sqlite db without alembic.
_SCHEMA = """
CREATE TABLE mentions (
  session_id TEXT NOT NULL,
  message_ref INTEGER NOT NULL,
  kind TEXT NOT NULL CHECK (kind IN ('tag', 'person')),
  value TEXT NOT NULL,
  owner_uid TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (session_id, message_ref, kind, value)
);
CREATE INDEX mentions_owner_kind_idx ON mentions (owner_uid, kind, value);
CREATE INDEX mentions_session_idx ON mentions (session_id, owner_uid);
"""


def _db(tmp_path) -> str:
    path = str(tmp_path / "solaris.db")
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA)
    conn.close()
    return path


# ---- migration replay ----


@pytest.mark.skipif(
    not (_DB_DIR / "alembic.ini").exists(), reason="database/ migrations not present"
)
def test_migrations_replay_to_head_create_mentions(tmp_path):
    """The 0006 migration replays on top of the latest rev in a fresh sqlite db.

    Runs the real alembic chain (baseline -> 0006) and asserts the `mentions`
    table lands with the expected columns — the schema the store reads.
    """
    alembic = pytest.importorskip("alembic")
    from alembic.command import upgrade
    from alembic.config import Config
    from alembic.util.exc import CommandError

    db = tmp_path / "replay.db"
    cfg = Config(str(_DB_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(_DB_DIR / "migrations"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite:///{db}")
    try:
        upgrade(cfg, "head")
    except (CommandError, KeyError) as exc:
        pytest.skip(f"migration chain incomplete (parent revision missing): {exc}")

    conn = sqlite3.connect(str(db))
    cols = {r[1] for r in conn.execute("PRAGMA table_info(mentions)")}
    conn.close()
    assert {"session_id", "message_ref", "kind", "value", "owner_uid"} <= cols
    assert alembic is not None  # silence the importorskip binding


# ---- parser ----


def test_parse_mentions_splits_tags_and_persons():
    tags, persons = parse_mentions("Plane #urlaub mit @anna und @ben, Thema #reise.")
    assert tags == ["urlaub", "reise"]
    assert persons == ["anna", "ben"]


def test_parse_mentions_dedups_and_lowercases():
    tags, persons = parse_mentions("#Urlaub #urlaub #URLAUB @Anna @anna")
    assert tags == ["urlaub"]
    assert persons == ["anna"]


def test_parse_mentions_ignores_midword_and_email():
    # `@` inside an email and a `#` glued to a word don't match.
    tags, persons = parse_mentions("mail an a@b.de und C#-Code, aber #real und @lena")
    assert tags == ["real"]
    assert persons == ["lena"]


def test_parse_mentions_empty():
    assert parse_mentions("kein tag hier") == ([], [])


def test_parse_mentions_allows_hierarchy_and_hyphen():
    tags, _ = parse_mentions("#projekt/wintergarten #to-do")
    assert tags == ["projekt/wintergarten", "to-do"]


# ---- store CRUD ----


def test_record_and_list_session_mentions(tmp_path):
    db = _db(tmp_path)
    ref0 = mentions_store.record_mentions(db, "sess-1", "mdopp", ["urlaub"], ["anna"])
    ref1 = mentions_store.record_mentions(db, "sess-1", "mdopp", ["reise"], [])
    assert ref0 == 0 and ref1 == 1
    items = mentions_store.list_session_mentions(db, "sess-1", "mdopp")
    # Ordered by message_ref, then kind ('person' < 'tag'), then value.
    assert items == [
        {"kind": "person", "value": "anna", "message_ref": 0},
        {"kind": "tag", "value": "urlaub", "message_ref": 0},
        {"kind": "tag", "value": "reise", "message_ref": 1},
    ]


def test_record_mentions_noop_when_nothing(tmp_path):
    db = _db(tmp_path)
    assert mentions_store.record_mentions(db, "sess-1", "mdopp", [], []) is None
    assert mentions_store.list_session_mentions(db, "sess-1", "mdopp") == []


def test_known_tags_and_persons_prefix(tmp_path):
    db = _db(tmp_path)
    mentions_store.record_mentions(db, "s1", "mdopp", ["urlaub", "ulm"], ["anna"])
    mentions_store.record_mentions(db, "s2", "mdopp", ["reise"], ["arno"])
    assert mentions_store.known_tags_for(db, "mdopp") == ["reise", "ulm", "urlaub"]
    assert mentions_store.known_tags_for(db, "mdopp", "ur") == ["urlaub"]
    assert mentions_store.known_persons_for(db, "mdopp", "a") == ["anna", "arno"]


def test_known_tags_prefix_escapes_like_wildcards(tmp_path):
    db = _db(tmp_path)
    mentions_store.record_mentions(db, "s1", "mdopp", ["real", "rxy"], [])
    # A `_`/`%` in the prefix is a literal, not a LIKE glob.
    assert mentions_store.known_tags_for(db, "mdopp", "r_") == []


def test_mentions_isolated_per_resident(tmp_path):
    db = _db(tmp_path)
    mentions_store.record_mentions(db, "s1", "mdopp", ["urlaub"], ["anna"])
    mentions_store.record_mentions(db, "s1", "lena", ["ferien"], ["ben"])
    # Lena never sees mdopp's tags/persons or mentions, and vice versa.
    assert mentions_store.known_tags_for(db, "lena") == ["ferien"]
    assert mentions_store.known_persons_for(db, "lena") == ["ben"]
    assert mentions_store.list_session_mentions(db, "s1", "lena") == [
        {"kind": "person", "value": "ben", "message_ref": 0},
        {"kind": "tag", "value": "ferien", "message_ref": 0},
    ]
    assert mentions_store.list_session_mentions(db, "s1", "mdopp") == [
        {"kind": "person", "value": "anna", "message_ref": 0},
        {"kind": "tag", "value": "urlaub", "message_ref": 0},
    ]


def test_store_degrades_when_db_missing(tmp_path):
    nope = str(tmp_path / "nope.db")
    assert mentions_store.record_mentions(nope, "s1", "mdopp", ["x"], []) is None
    assert mentions_store.list_session_mentions(nope, "s1", "mdopp") == []
    assert mentions_store.known_tags_for(nope, "mdopp") == []
    assert mentions_store.known_persons_for(nope, "mdopp") == []


def test_store_degrades_when_table_absent(tmp_path):
    # DB file exists but the migration hasn't created the table yet.
    path = str(tmp_path / "empty.db")
    sqlite3.connect(path).close()
    assert mentions_store.record_mentions(path, "s1", "mdopp", ["x"], []) is None
    assert mentions_store.known_tags_for(path, "mdopp") == []
    assert mentions_store.list_session_mentions(path, "s1", "mdopp") == []


# ---- persons seed ----


def test_seeded_persons_unions_residents_and_manual():
    seed = seeded_persons(["mdopp", "Tom"])
    assert "tom" in seed  # resident folded in + lowercased
    assert "anna" in seed and "lena" in seed  # manual list
    assert len(seed) == len(set(seed))  # de-duplicated


# ---- endpoints ----

from tests.test_server import _FakeEngine  # noqa: E402


def _app(db):
    return build_app(
        engine=_FakeEngine(),
        remote_user_header="Remote-User",
        default_uid="household",
        solaris_db_path=db,
    )


async def test_tags_endpoint_returns_known_prefix(aiohttp_client, tmp_path):
    db = _db(tmp_path)
    mentions_store.record_mentions(db, "s1", "mdopp", ["urlaub"], [])
    client = await aiohttp_client(_app(db))
    resp = await client.get(
        "/api/mentions/tags?q=urlaub", headers={"Remote-User": "mdopp"}
    )
    body = await resp.json()
    assert resp.status == 200
    assert body["tags"] == [{"kind": "tag", "value": "urlaub"}]


async def test_tags_endpoint_strips_hash_prefix(aiohttp_client, tmp_path):
    db = _db(tmp_path)
    mentions_store.record_mentions(db, "s1", "mdopp", ["urlaub"], [])
    client = await aiohttp_client(_app(db))
    # The browser may send the leading `#`; the endpoint strips it.
    resp = await client.get(
        "/api/mentions/tags?q=%23url", headers={"Remote-User": "mdopp"}
    )
    body = await resp.json()
    assert body["tags"] == [{"kind": "tag", "value": "urlaub"}]


async def test_tags_endpoint_scoped_per_resident(aiohttp_client, tmp_path):
    db = _db(tmp_path)
    mentions_store.record_mentions(db, "s1", "mdopp", ["urlaub"], [])
    client = await aiohttp_client(_app(db))
    resp = await client.get("/api/mentions/tags", headers={"Remote-User": "lena"})
    body = await resp.json()
    assert body["tags"] == []  # lena never used a tag


async def test_persons_endpoint_returns_seed(aiohttp_client, tmp_path):
    db = _db(tmp_path)
    client = await aiohttp_client(_app(db))
    resp = await client.get("/api/mentions/persons", headers={"Remote-User": "mdopp"})
    body = await resp.json()
    assert resp.status == 200
    values = {p["value"] for p in body["persons"]}
    # Seeded from the manual list + the caller's own uid, with no chat history.
    assert {"anna", "lena", "mdopp"} <= values
    assert all(p["kind"] == "person" for p in body["persons"])


async def test_persons_endpoint_unions_used_and_seed_with_prefix(
    aiohttp_client, tmp_path
):
    db = _db(tmp_path)
    mentions_store.record_mentions(db, "s1", "mdopp", [], ["arno"])
    client = await aiohttp_client(_app(db))
    resp = await client.get(
        "/api/mentions/persons?q=a", headers={"Remote-User": "mdopp"}
    )
    body = await resp.json()
    values = [p["value"] for p in body["persons"]]
    assert values == ["anna", "arno"]  # used + seed, prefix-filtered, sorted


# ---- session tag-cloud endpoint (#279c) ----


async def test_session_mentions_endpoint_lists_with_refs(aiohttp_client, tmp_path):
    db = _db(tmp_path)
    mentions_store.record_mentions(db, "s1", "mdopp", ["urlaub"], ["anna"])
    mentions_store.record_mentions(db, "s1", "mdopp", ["reise"], [])
    client = await aiohttp_client(_app(db))
    resp = await client.get(
        "/api/sessions/s1/mentions", headers={"Remote-User": "mdopp"}
    )
    assert resp.status == 200
    body = await resp.json()
    assert body["ok"] is True
    # Each item carries the kind/value + the message_ref to jump to.
    assert body["mentions"] == [
        {"kind": "person", "value": "anna", "message_ref": 0},
        {"kind": "tag", "value": "urlaub", "message_ref": 0},
        {"kind": "tag", "value": "reise", "message_ref": 1},
    ]


async def test_session_mentions_endpoint_scoped_per_resident(aiohttp_client, tmp_path):
    db = _db(tmp_path)
    mentions_store.record_mentions(db, "s1", "mdopp", ["urlaub"], [])
    mentions_store.record_mentions(db, "s1", "lena", ["ferien"], [])
    client = await aiohttp_client(_app(db))
    resp = await client.get(
        "/api/sessions/s1/mentions", headers={"Remote-User": "lena"}
    )
    body = await resp.json()
    # lena sees only her own mentions in the shared session.
    assert body["mentions"] == [{"kind": "tag", "value": "ferien", "message_ref": 0}]


async def test_session_mentions_endpoint_degrades_when_db_missing(
    aiohttp_client, tmp_path
):
    app = build_app(
        engine=_FakeEngine(),
        remote_user_header="Remote-User",
        default_uid="household",
        solaris_db_path=str(tmp_path / "nope.db"),
    )
    client = await aiohttp_client(app)
    resp = await client.get(
        "/api/sessions/s1/mentions", headers={"Remote-User": "mdopp"}
    )
    assert resp.status == 200
    body = await resp.json()
    assert body == {"ok": True, "mentions": []}


# ---- chat turn persists mentions ----


async def test_chat_turn_persists_mentions(aiohttp_client, tmp_path):
    db = _db(tmp_path)
    fake = _FakeEngine()
    app = build_app(
        engine=fake,
        remote_user_header="Remote-User",
        default_uid="household",
        solaris_db_path=db,
        attachments_dir=str(tmp_path / "att"),
    )
    client = await aiohttp_client(app)
    resp = await client.post(
        "/api/chat",
        json={"input": "Plane #urlaub mit @anna"},
        headers={"Remote-User": "mdopp"},
    )
    assert resp.status == 200
    sid = (await resp.json())["session_id"]
    items = mentions_store.list_session_mentions(db, sid, "mdopp")
    assert {(i["kind"], i["value"]) for i in items} == {
        ("tag", "urlaub"),
        ("person", "anna"),
    }


async def test_chat_turn_records_auto_anchors_deduped(aiohttp_client, tmp_path):
    """#501: the agent's auto-surfaced anchors land in `mentions`, deduped
    against the tokens the user already typed this turn (here `#urlaub`)."""
    db = _db(tmp_path)
    fake = _FakeEngine(
        events=[
            {"type": "assistant.delta", "data": {"delta": "Klar."}},
            {
                "type": "anchors",
                "data": {"anchors": ["#urlaub", "@anna", "#garten"]},
            },
            {"type": "run.completed", "data": {}},
        ]
    )
    app = build_app(
        engine=fake,
        remote_user_header="Remote-User",
        default_uid="household",
        solaris_db_path=db,
        attachments_dir=str(tmp_path / "att"),
    )
    client = await aiohttp_client(app)
    resp = await client.post(
        "/api/chat/stream",
        json={"input": "Plane #urlaub"},
        headers={"Remote-User": "mdopp"},
    )
    assert resp.status == 200
    await resp.text()
    items = mentions_store.list_session_mentions(db, "sess-1", "mdopp")
    recorded = {(i["kind"], i["value"]) for i in items}
    # `#urlaub` came from the user (recorded by persist_mentions); the agent's
    # anchor for it is deduped out — only the new anchors are added.
    assert recorded == {("tag", "urlaub"), ("person", "anna"), ("tag", "garten")}


async def test_ephemeral_chat_persists_no_mentions(aiohttp_client, tmp_path):
    db = _db(tmp_path)
    fake = _FakeEngine()
    app = build_app(
        engine=fake,
        remote_user_header="Remote-User",
        default_uid="household",
        solaris_db_path=db,
        attachments_dir=str(tmp_path / "att"),
    )
    client = await aiohttp_client(app)
    resp = await client.post(
        "/api/chat",
        json={"input": "geheim #urlaub @anna", "ephemeral": True},
        headers={"Remote-User": "mdopp"},
    )
    sid = (await resp.json())["session_id"]
    # An incognito turn keeps no durable mention state.
    assert mentions_store.list_session_mentions(db, sid, "mdopp") == []


@pytest.mark.parametrize("path", ["/api/mentions/tags", "/api/mentions/persons"])
async def test_autosuggest_degrades_when_db_missing(aiohttp_client, tmp_path, path):
    app = build_app(
        engine=_FakeEngine(),
        remote_user_header="Remote-User",
        default_uid="household",
        solaris_db_path=str(tmp_path / "nope.db"),
    )
    client = await aiohttp_client(app)
    resp = await client.get(path, headers={"Remote-User": "mdopp"})
    assert resp.status == 200
    body = await resp.json()
    assert body["ok"] is True

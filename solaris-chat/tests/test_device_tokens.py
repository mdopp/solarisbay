"""Device-token store + endpoints + resolve_uid precedence (#717).

Covers: create returns a `sol_device_`-prefixed token and stores ONLY a hash
(never the plaintext); resolve maps a valid token to its owner_uid; a
revoked/unknown/malformed token resolves to None (fail-closed, NOT default_uid);
list is metadata-only (no hash/plaintext); revoke is owner-checked; the
create/list/revoke endpoints require the interactive Authelia session
(Remote-User) and reject a device-token bearer; and resolve_uid keeps the
Remote-User + service-key behaviour UNCHANGED when no device token is presented
while a `sol_device_` bearer resolves to the token owner.

The table migration 0021 creates is replayed locally with raw SQL — a chat test
must NOT import alembic (CI runs solaris-chat in a clean env without it).
"""

from __future__ import annotations

import hashlib
import sqlite3

from solaris_chat import device_token_store
from solaris_chat.server import build_app, resolve_uid

# The table migration 0021 creates, replayed locally (no alembic).
_SCHEMA = """
CREATE TABLE device_tokens (
  id         TEXT PRIMARY KEY,
  owner_uid  TEXT NOT NULL,
  token_hash TEXT NOT NULL UNIQUE,
  label      TEXT,
  created    TEXT NOT NULL DEFAULT (datetime('now')),
  last_used  TEXT,
  revoked    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX device_tokens_hash_idx ON device_tokens (token_hash);
CREATE INDEX device_tokens_owner_idx ON device_tokens (owner_uid);
"""


def _db(tmp_path) -> str:
    path = str(tmp_path / "solaris.db")
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA)
    conn.commit()
    conn.close()
    return path


class _FakeEngine:
    async def dispatch_tool(self, name, arguments):  # pragma: no cover - unused
        return "{}"


class _FakeRequest:
    """Minimal request stub exposing only `.headers` for resolve_uid."""

    def __init__(self, headers):
        self.headers = headers


# ---- store ----------------------------------------------------------------


def test_store_degrades_to_empty_without_table(tmp_path):
    assert device_token_store.list_for_uid(str(tmp_path / "nope.db"), "mdopp") == []
    assert device_token_store.resolve(str(tmp_path / "nope.db"), "sol_device_x") is None


def test_create_returns_prefixed_token_and_stores_only_hash(tmp_path):
    db = _db(tmp_path)
    token_id, token = device_token_store.create(db, "mdopp", "Pixel widget")
    assert token.startswith("sol_device_")
    conn = sqlite3.connect(db)
    row = conn.execute(
        "SELECT owner_uid, token_hash, label FROM device_tokens WHERE id = ?",
        (token_id,),
    ).fetchone()
    conn.close()
    owner, stored_hash, label = row
    assert owner == "mdopp" and label == "Pixel widget"
    # NEVER the plaintext at rest — only its sha256 hex.
    assert stored_hash == hashlib.sha256(token.encode()).hexdigest()
    assert token not in stored_hash


def test_resolve_maps_valid_token_to_owner(tmp_path):
    db = _db(tmp_path)
    _, token = device_token_store.create(db, "lena")
    assert device_token_store.resolve(db, token) == "lena"


def test_resolve_stamps_last_used(tmp_path):
    db = _db(tmp_path)
    tid, token = device_token_store.create(db, "lena")
    device_token_store.resolve(db, token)
    row = device_token_store.list_for_uid(db, "lena")[0]
    assert row["id"] == tid and row["last_used"] is not None


def test_revoked_token_fails_closed(tmp_path):
    db = _db(tmp_path)
    tid, token = device_token_store.create(db, "mdopp")
    assert device_token_store.revoke(db, "mdopp", tid) is True
    assert device_token_store.resolve(db, token) is None


def test_invalid_and_malformed_tokens_resolve_none(tmp_path):
    db = _db(tmp_path)
    assert device_token_store.resolve(db, "sol_device_unknown") is None
    assert device_token_store.resolve(db, "not_a_device_token") is None
    assert device_token_store.resolve(db, "") is None


def test_list_is_metadata_only(tmp_path):
    db = _db(tmp_path)
    device_token_store.create(db, "mdopp", "widget")
    rows = device_token_store.list_for_uid(db, "mdopp")
    assert len(rows) == 1
    assert set(rows[0]) == {
        "id",
        "owner_uid",
        "label",
        "created",
        "last_used",
        "revoked",
    }
    assert "token_hash" not in rows[0]
    assert "token" not in rows[0]


def test_revoke_is_owner_checked(tmp_path):
    db = _db(tmp_path)
    tid, token = device_token_store.create(db, "lena")
    # mdopp cannot revoke lena's token by guessing its id.
    assert device_token_store.revoke(db, "mdopp", tid) is False
    assert device_token_store.resolve(db, token) == "lena"


# ---- resolve_uid precedence ----------------------------------------------


def test_resolve_uid_unchanged_for_remote_user_header(tmp_path):
    db = _db(tmp_path)
    req = _FakeRequest({"Remote-User": "mdopp"})
    assert resolve_uid(req, "Remote-User", "household", db) == "mdopp"


def test_resolve_uid_service_key_bearer_unchanged(tmp_path):
    db = _db(tmp_path)
    # A non-device bearer (e.g. SOLARIS_API_KEY) is left untouched: header wins,
    # else default — exactly as before.
    req = _FakeRequest(
        {"Authorization": "Bearer SOLARIS_API_KEY", "Remote-User": "mdopp"}
    )
    assert resolve_uid(req, "Remote-User", "household", db) == "mdopp"
    req2 = _FakeRequest({"Authorization": "Bearer SOLARIS_API_KEY"})
    assert resolve_uid(req2, "Remote-User", "household", db) == "household"


def test_resolve_uid_absent_header_falls_back_to_default(tmp_path):
    db = _db(tmp_path)
    assert resolve_uid(_FakeRequest({}), "Remote-User", "household", db) == "household"


def test_resolve_uid_device_bearer_resolves_to_owner(tmp_path):
    db = _db(tmp_path)
    _, token = device_token_store.create(db, "lena")
    req = _FakeRequest({"Authorization": f"Bearer {token}"})
    assert resolve_uid(req, "Remote-User", "household", db) == "lena"


def test_resolve_uid_invalid_device_bearer_is_fail_closed(tmp_path):
    db = _db(tmp_path)
    # An unknown/revoked device token must NOT fall through to default_uid.
    req = _FakeRequest({"Authorization": "Bearer sol_device_bogus"})
    assert resolve_uid(req, "Remote-User", "household", db) == ""
    tid, token = device_token_store.create(db, "lena")
    device_token_store.revoke(db, "lena", tid)
    req2 = _FakeRequest({"Authorization": f"Bearer {token}"})
    assert resolve_uid(req2, "Remote-User", "household", db) == ""


# ---- endpoints ------------------------------------------------------------


def _app(tmp_path, db):
    return build_app(
        hermes=_FakeEngine(),
        remote_user_header="Remote-User",
        default_uid="household",
        solaris_db_path=db,
        notes_dir=str(tmp_path),
    )


async def test_create_endpoint_requires_interactive_session(aiohttp_client, tmp_path):
    db = _db(tmp_path)
    client = await aiohttp_client(_app(tmp_path, db))
    r = await client.post("/api/device-tokens", json={"label": "x"})
    assert r.status == 401
    assert device_token_store.list_for_uid(db, "household") == []


async def test_create_endpoint_rejects_device_token_bearer(aiohttp_client, tmp_path):
    db = _db(tmp_path)
    _, token = device_token_store.create(db, "mdopp")
    client = await aiohttp_client(_app(tmp_path, db))
    # A device token must NOT be usable to mint another device token.
    r = await client.post(
        "/api/device-tokens",
        json={"label": "y"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status == 401
    assert len(device_token_store.list_for_uid(db, "mdopp")) == 1


async def test_create_endpoint_returns_token_once(aiohttp_client, tmp_path):
    db = _db(tmp_path)
    client = await aiohttp_client(_app(tmp_path, db))
    r = await client.post(
        "/api/device-tokens", json={"label": "widget"}, headers={"Remote-User": "mdopp"}
    )
    assert r.status == 200
    j = await r.json()
    assert j["ok"] and j["token"].startswith("sol_device_") and j["id"]
    # The minted token authenticates as its owner.
    assert device_token_store.resolve(db, j["token"]) == "mdopp"


async def test_list_endpoint_owner_scoped_metadata_only(aiohttp_client, tmp_path):
    db = _db(tmp_path)
    device_token_store.create(db, "mdopp", "a")
    device_token_store.create(db, "lena", "b")
    client = await aiohttp_client(_app(tmp_path, db))
    j = await (
        await client.get("/api/device-tokens", headers={"Remote-User": "mdopp"})
    ).json()
    assert len(j["tokens"]) == 1 and j["tokens"][0]["label"] == "a"
    assert "token_hash" not in j["tokens"][0]


async def test_revoke_endpoint_is_owner_checked(aiohttp_client, tmp_path):
    db = _db(tmp_path)
    tid, token = device_token_store.create(db, "lena")
    client = await aiohttp_client(_app(tmp_path, db))
    # mdopp cannot revoke lena's token.
    r = await client.delete(
        f"/api/device-tokens/{tid}", headers={"Remote-User": "mdopp"}
    )
    assert r.status == 404
    assert device_token_store.resolve(db, token) == "lena"
    # lena can.
    r2 = await client.delete(
        f"/api/device-tokens/{tid}", headers={"Remote-User": "lena"}
    )
    assert r2.status == 200
    assert device_token_store.resolve(db, token) is None


# ---- /pair-device page (#751) ---------------------------------------------


async def test_pair_device_page_renders_and_mints_nothing(aiohttp_client, tmp_path):
    db = _db(tmp_path)
    client = await aiohttp_client(_app(tmp_path, db))
    r = await client.get("/pair-device", headers={"Remote-User": "mdopp"})
    assert r.status == 200
    assert "Dieses Gerät koppeln" in await r.text()
    # A GET must NOT mint a token (drive-by / CSRF protection).
    assert device_token_store.list_for_uid(db, "mdopp") == []


async def test_pair_device_page_rejects_without_remote_user(aiohttp_client, tmp_path):
    db = _db(tmp_path)
    client = await aiohttp_client(_app(tmp_path, db))
    r = await client.get("/pair-device")
    assert r.status == 401
    assert device_token_store.list_for_uid(db, "household") == []


async def test_pair_device_confirm_mints_one_and_deep_link_redirects(
    aiohttp_client, tmp_path
):
    db = _db(tmp_path)
    client = await aiohttp_client(_app(tmp_path, db))
    r = await client.post(
        "/pair-device",
        data={"label": "Mein Pixel"},
        headers={"Remote-User": "mdopp"},
        allow_redirects=False,
    )
    assert r.status == 302
    tokens = device_token_store.list_for_uid(db, "mdopp")
    assert len(tokens) == 1 and tokens[0]["label"] == "Mein Pixel"
    loc = r.headers["Location"]
    assert loc.startswith("cloud.dopp.solaris://pair#token=sol_device_")
    assert f"&id={tokens[0]['id']}" in loc


async def test_pair_device_confirm_rejects_device_token_bearer(
    aiohttp_client, tmp_path
):
    db = _db(tmp_path)
    _, token = device_token_store.create(db, "mdopp")
    client = await aiohttp_client(_app(tmp_path, db))
    # A device token / service key can NOT mint via the page (fail-closed).
    r = await client.post(
        "/pair-device",
        data={"label": "x"},
        headers={"Authorization": f"Bearer {token}"},
        allow_redirects=False,
    )
    assert r.status == 401
    assert len(device_token_store.list_for_uid(db, "mdopp")) == 1

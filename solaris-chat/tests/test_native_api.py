"""The `/napi/` native-API prefix — strict device-token auth, fail-closed (#757).

The Android widgets reach solaris-chat through an Authelia BYPASS on this prefix,
so `/napi/*` is device-token-ONLY: a request without a valid `sol_device_` bearer
is 401 and must NEVER inherit the household `default_uid` (an unauthenticated
internet caller could otherwise control the house) nor trust a `Remote-User`
header. This mirrors the `/api/` handlers but with the strict native gate; the
`/api/` routes keep their unchanged loopback/browser behaviour.

Reuses the #748 device-token store; replays the migration-0021 schema with raw
SQL (a chat test must NOT import alembic).
"""

from __future__ import annotations

import sqlite3

from solaris_chat import device_token_store
from solaris_chat.server import build_app, native_uid

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
    def __init__(self, headers, path):
        self.headers = headers
        self.path = path


def _app(tmp_path, db):
    return build_app(
        hermes=_FakeEngine(),
        remote_user_header="Remote-User",
        default_uid="household",
        solaris_db_path=db,
        notes_dir=str(tmp_path),
    )


def _ha_app(tmp_path, db):
    return build_app(
        hermes=_FakeEngine(),
        remote_user_header="Remote-User",
        default_uid="household",
        solaris_db_path=db,
        notes_dir=str(tmp_path),
        hass_url="http://ha",
        hass_token="t",
    )


# ---- native_uid helper (fail-closed, device-token-only) -------------------


def test_native_uid_none_without_bearer(tmp_path):
    db = _db(tmp_path)
    # A Remote-User header must NOT authenticate a `/napi/` request.
    req = _FakeRequest({"Remote-User": "mdopp"}, "/napi/whoami")
    assert native_uid(req, db) is None


def test_native_uid_none_for_service_key(tmp_path):
    db = _db(tmp_path)
    req = _FakeRequest({"Authorization": "Bearer SOLARIS_API_KEY"}, "/napi/whoami")
    assert native_uid(req, db) is None


def test_native_uid_none_for_revoked_token(tmp_path):
    db = _db(tmp_path)
    tid, token = device_token_store.create(db, "lena")
    device_token_store.revoke(db, "lena", tid)
    req = _FakeRequest({"Authorization": f"Bearer {token}"}, "/napi/whoami")
    assert native_uid(req, db) is None


def test_native_uid_resolves_owner_for_valid_token(tmp_path):
    db = _db(tmp_path)
    _, token = device_token_store.create(db, "lena")
    req = _FakeRequest({"Authorization": f"Bearer {token}"}, "/napi/whoami")
    assert native_uid(req, db) == "lena"


# ---- /napi/whoami ----------------------------------------------------------


async def test_napi_whoami_without_bearer_is_401_not_household(
    aiohttp_client, tmp_path
):
    db = _db(tmp_path)
    client = await aiohttp_client(_app(tmp_path, db))
    r = await client.get("/napi/whoami")
    assert r.status == 401
    j = await r.json()
    # Fail-closed: NOT the household default_uid.
    assert j == {"ok": False, "error": "unauthorized"}


async def test_napi_whoami_remote_user_header_is_401(aiohttp_client, tmp_path):
    db = _db(tmp_path)
    client = await aiohttp_client(_app(tmp_path, db))
    # Authelia bypasses this prefix, so a spoofable Remote-User must NOT count.
    r = await client.get("/napi/whoami", headers={"Remote-User": "mdopp"})
    assert r.status == 401


async def test_napi_whoami_service_key_bearer_is_401(aiohttp_client, tmp_path):
    db = _db(tmp_path)
    client = await aiohttp_client(_app(tmp_path, db))
    r = await client.get(
        "/napi/whoami", headers={"Authorization": "Bearer SOLARIS_API_KEY"}
    )
    assert r.status == 401


async def test_napi_whoami_revoked_token_is_401(aiohttp_client, tmp_path):
    db = _db(tmp_path)
    tid, token = device_token_store.create(db, "lena")
    device_token_store.revoke(db, "lena", tid)
    client = await aiohttp_client(_app(tmp_path, db))
    r = await client.get("/napi/whoami", headers={"Authorization": f"Bearer {token}"})
    assert r.status == 401


async def test_napi_whoami_valid_token_resolves_owner(aiohttp_client, tmp_path):
    db = _db(tmp_path)
    _, token = device_token_store.create(db, "lena")
    client = await aiohttp_client(_app(tmp_path, db))
    r = await client.get("/napi/whoami", headers={"Authorization": f"Bearer {token}"})
    assert r.status == 200
    j = await r.json()
    assert j["ok"] is True and j["uid"] == "lena"
    # whoami still carries the VAPID key the widget subscribes with.
    assert "vapid_public_key" in j


# ---- a data endpoint: /napi/portal/entity-history --------------------------


async def test_napi_entity_history_without_bearer_is_401(aiohttp_client, tmp_path):
    db = _db(tmp_path)
    client = await aiohttp_client(_ha_app(tmp_path, db))
    r = await client.get("/napi/portal/entity-history?entity_id=sensor.temp&range=24h")
    assert r.status == 401


async def test_napi_entity_history_valid_token_is_200(
    aiohttp_client, tmp_path, monkeypatch
):
    db = _db(tmp_path)
    _, token = device_token_store.create(db, "lena")

    async def _fake(url, tok, entity_id, rng):
        return [{"t": "t0", "state": "21.2"}]

    monkeypatch.setattr("solaris_chat.server.fetch_entity_history", _fake)
    client = await aiohttp_client(_ha_app(tmp_path, db))
    r = await client.get(
        "/napi/portal/entity-history?entity_id=sensor.temp&range=24h",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status == 200
    assert (await r.json())["history"] == [{"t": "t0", "state": "21.2"}]


# ---- /napi/device-tokens: scoped by the device-token owner, not Remote-User


async def test_napi_device_tokens_list_scoped_to_token_owner(aiohttp_client, tmp_path):
    db = _db(tmp_path)
    device_token_store.create(db, "lena", "a")
    device_token_store.create(db, "mdopp", "b")
    _, token = device_token_store.create(db, "lena", "widget")
    client = await aiohttp_client(_app(tmp_path, db))
    r = await client.get(
        "/napi/device-tokens", headers={"Authorization": f"Bearer {token}"}
    )
    assert r.status == 200
    labels = {t["label"] for t in (await r.json())["tokens"]}
    # Only lena's tokens, never mdopp's.
    assert labels == {"a", "widget"}


async def test_napi_device_tokens_list_without_bearer_is_401(aiohttp_client, tmp_path):
    db = _db(tmp_path)
    client = await aiohttp_client(_app(tmp_path, db))
    r = await client.get("/napi/device-tokens", headers={"Remote-User": "mdopp"})
    assert r.status == 401


async def test_napi_device_tokens_revoke_owner_checked(aiohttp_client, tmp_path):
    db = _db(tmp_path)
    other_id, other = device_token_store.create(db, "mdopp")
    _, token = device_token_store.create(db, "lena")
    client = await aiohttp_client(_app(tmp_path, db))
    # lena's device token cannot revoke mdopp's.
    r = await client.delete(
        f"/napi/device-tokens/{other_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status == 404
    assert device_token_store.resolve(db, other) == "mdopp"


# ---- minting is NOT reachable under /napi/ ---------------------------------


async def test_napi_does_not_expose_token_minting(aiohttp_client, tmp_path):
    db = _db(tmp_path)
    _, token = device_token_store.create(db, "lena")
    client = await aiohttp_client(_app(tmp_path, db))
    # POST /napi/device-tokens is not routed at all — minting stays interactive-
    # Authelia-only (#748/#751), so a device token can't mint another.
    r = await client.post(
        "/napi/device-tokens",
        json={"label": "x"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status in (404, 405)
    # /pair-device likewise is not on the native prefix.
    r2 = await client.post(
        "/napi/pair-device",
        data={"label": "x"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r2.status in (404, 405)
    assert len(device_token_store.list_for_uid(db, "lena")) == 1


# ---- /api/* stays unchanged: falls back to default_uid ---------------------


async def test_api_whoami_still_falls_back_to_default_uid(aiohttp_client, tmp_path):
    db = _db(tmp_path)
    client = await aiohttp_client(_app(tmp_path, db))
    # The browser/loopback path is untouched: no bearer ⇒ household default_uid.
    r = await client.get("/api/whoami")
    assert r.status == 200
    assert (await r.json())["uid"] == "household"

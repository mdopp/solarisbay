"""Web Push store + endpoints + notifier + scheduler wiring (#713).

Covers: push_store dedup (upsert by endpoint), owner-scope of list_for_uid,
prune by endpoint; POST /api/push/(un)subscribe owner-scoping + 400; /api/whoami
surfaces the VAPID public key; the Notifier fans out per subscription, prunes on
a 410, and swallows any other error (never breaks the timer loop); a fired timer
enqueues a push via the injected notifier. The table is created with raw SQL
copied from migration 0020 — a chat test must NOT import alembic (CI runs
solaris-chat in a clean env without it).
"""

from __future__ import annotations

import sqlite3

import pytest

from solaris_chat import device_token_store, push_store
from solaris_chat.engine.notify import EventBus, Notifier
from solaris_chat.engine.scheduler import TimerScheduler
from solaris_chat.engine.sb_events import SbApprovalEventBridge
from solaris_chat.server import build_app

# The table migration 0020 creates, replayed locally (no alembic).
_SCHEMA = """
CREATE TABLE push_subscriptions (
  id         TEXT PRIMARY KEY,
  owner_uid  TEXT NOT NULL,
  endpoint   TEXT NOT NULL UNIQUE,
  p256dh     TEXT NOT NULL,
  auth       TEXT NOT NULL,
  user_agent TEXT NOT NULL DEFAULT '',
  created    TEXT NOT NULL DEFAULT (datetime('now')),
  last_ok    TEXT
);
CREATE INDEX push_subscriptions_owner_idx ON push_subscriptions (owner_uid);
"""

# The device-token table migration 0021 creates (for the /napi device-token gate).
_DEVICE_TOKENS_SCHEMA = """
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


def _db_with_device_tokens(tmp_path) -> str:
    path = _db(tmp_path)
    conn = sqlite3.connect(path)
    conn.executescript(_DEVICE_TOKENS_SCHEMA)
    conn.commit()
    conn.close()
    return path


class _FakeEngine:
    async def dispatch_tool(self, name, arguments):  # pragma: no cover - unused here
        return "{}"


# ---- store ----------------------------------------------------------------


def test_store_degrades_to_empty_without_table(tmp_path):
    assert push_store.list_for_uid(str(tmp_path / "nope.db"), "mdopp") == []


def test_upsert_dedupes_by_endpoint(tmp_path):
    db = _db(tmp_path)
    push_store.upsert(db, "mdopp", "https://push/1", "p1", "a1", "UA")
    push_store.upsert(db, "mdopp", "https://push/1", "p2", "a2", "UA2")
    subs = push_store.list_for_uid(db, "mdopp")
    assert len(subs) == 1
    assert subs[0]["p256dh"] == "p2" and subs[0]["auth"] == "a2"


def test_list_is_owner_scoped(tmp_path):
    db = _db(tmp_path)
    push_store.upsert(db, "mdopp", "https://push/1", "p", "a")
    push_store.upsert(db, "lena", "https://push/2", "p", "a")
    assert [s["endpoint"] for s in push_store.list_for_uid(db, "mdopp")] == [
        "https://push/1"
    ]


def test_remove_by_endpoint_prunes(tmp_path):
    db = _db(tmp_path)
    push_store.upsert(db, "mdopp", "https://push/1", "p", "a")
    assert push_store.remove_by_endpoint(db, "https://push/1") == 1
    assert push_store.list_for_uid(db, "mdopp") == []


def test_mark_ok_stamps_last_ok(tmp_path):
    db = _db(tmp_path)
    push_store.upsert(db, "mdopp", "https://push/1", "p", "a")
    push_store.mark_ok(db, "https://push/1")
    assert push_store.list_for_uid(db, "mdopp")[0]["last_ok"] is not None


# ---- endpoints ------------------------------------------------------------


def _app(tmp_path, db, **kw):
    return build_app(
        hermes=_FakeEngine(),
        remote_user_header="Remote-User",
        default_uid="household",
        solaris_db_path=db,
        notes_dir=str(tmp_path),
        **kw,
    )


async def test_subscribe_is_owner_scoped(aiohttp_client, tmp_path):
    db = _db(tmp_path)
    client = await aiohttp_client(_app(tmp_path, db))
    body = {"endpoint": "https://push/1", "keys": {"p256dh": "p", "auth": "a"}}
    r = await client.post(
        "/api/push/subscribe", json=body, headers={"Remote-User": "mdopp"}
    )
    assert r.status == 200
    subs = push_store.list_for_uid(db, "mdopp")
    assert subs[0]["owner_uid"] == "mdopp"
    assert push_store.list_for_uid(db, "lena") == []


async def test_subscribe_rejects_incomplete_body(aiohttp_client, tmp_path):
    db = _db(tmp_path)
    client = await aiohttp_client(_app(tmp_path, db))
    r = await client.post(
        "/api/push/subscribe",
        json={"endpoint": "https://push/1"},
        headers={"Remote-User": "mdopp"},
    )
    assert r.status == 400


async def test_unsubscribe_only_removes_own_endpoint(aiohttp_client, tmp_path):
    db = _db(tmp_path)
    push_store.upsert(db, "lena", "https://push/lena", "p", "a")
    client = await aiohttp_client(_app(tmp_path, db))
    # mdopp cannot drop lena's device by guessing its endpoint.
    r = await client.post(
        "/api/push/unsubscribe",
        json={"endpoint": "https://push/lena"},
        headers={"Remote-User": "mdopp"},
    )
    assert r.status == 200
    assert len(push_store.list_for_uid(db, "lena")) == 1


# ---- /napi/push/* (device-token, fail-closed, owner-scoped, #843) ---------


async def test_napi_subscribe_without_token_is_401(aiohttp_client, tmp_path):
    db = _db_with_device_tokens(tmp_path)
    client = await aiohttp_client(_app(tmp_path, db))
    body = {"endpoint": "https://up/1", "keys": {"p256dh": "p", "auth": "a"}}
    # No `sol_device_` bearer, and a Remote-User header must NOT authenticate.
    r = await client.post(
        "/napi/push/subscribe", json=body, headers={"Remote-User": "mdopp"}
    )
    assert r.status == 401
    assert push_store.list_for_uid(db, "mdopp") == []


async def test_napi_subscribe_stores_owner_scoped(aiohttp_client, tmp_path):
    db = _db_with_device_tokens(tmp_path)
    _, token = device_token_store.create(db, "mdopp")
    client = await aiohttp_client(_app(tmp_path, db))
    body = {"endpoint": "https://up/1", "keys": {"p256dh": "p", "auth": "a"}}
    r = await client.post(
        "/napi/push/subscribe",
        json=body,
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status == 200
    subs = push_store.list_for_uid(db, "mdopp")
    assert subs[0]["owner_uid"] == "mdopp" and subs[0]["endpoint"] == "https://up/1"
    assert push_store.list_for_uid(db, "lena") == []


async def test_napi_unsubscribe_removes_owner_scoped(aiohttp_client, tmp_path):
    db = _db_with_device_tokens(tmp_path)
    _, token = device_token_store.create(db, "mdopp")
    push_store.upsert(db, "mdopp", "https://up/1", "p", "a")
    client = await aiohttp_client(_app(tmp_path, db))
    r = await client.post(
        "/napi/push/unsubscribe",
        json={"endpoint": "https://up/1"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert r.status == 200
    assert push_store.list_for_uid(db, "mdopp") == []


async def test_napi_and_api_subscribe_share_one_store(aiohttp_client, tmp_path):
    db = _db_with_device_tokens(tmp_path)
    _, token = device_token_store.create(db, "mdopp")
    client = await aiohttp_client(_app(tmp_path, db))
    # /napi (device token) and /api (Remote-User) write the SAME push_store row set.
    await client.post(
        "/napi/push/subscribe",
        json={"endpoint": "https://up/napi", "keys": {"p256dh": "p", "auth": "a"}},
        headers={"Authorization": f"Bearer {token}"},
    )
    await client.post(
        "/api/push/subscribe",
        json={"endpoint": "https://up/api", "keys": {"p256dh": "p", "auth": "a"}},
        headers={"Remote-User": "mdopp"},
    )
    assert sorted(s["endpoint"] for s in push_store.list_for_uid(db, "mdopp")) == [
        "https://up/api",
        "https://up/napi",
    ]


async def test_whoami_returns_vapid_public_key(aiohttp_client, tmp_path):
    db = _db(tmp_path)
    client = await aiohttp_client(_app(tmp_path, db, vapid_public_key="PUBKEY"))
    j = await (await client.get("/api/whoami", headers={"Remote-User": "mdopp"})).json()
    assert j["vapid_public_key"] == "PUBKEY"


# ---- Notifier -------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status_code):
        self.status_code = status_code


class _FakeWebPushException(Exception):
    def __init__(self, status_code):
        super().__init__("boom")
        self.response = _FakeResponse(status_code)


def _fake_pywebpush(monkeypatch, calls, raise_status=None):
    """Install a fake `pywebpush` module the Notifier imports lazily."""
    import sys
    import types

    def webpush(*, subscription_info, data, vapid_private_key, vapid_claims):
        calls.append(subscription_info["endpoint"])
        if raise_status is not None:
            raise _FakeWebPushException(raise_status)

    mod = types.SimpleNamespace(webpush=webpush, WebPushException=_FakeWebPushException)
    monkeypatch.setitem(sys.modules, "pywebpush", mod)


# A fixed P-256 keypair (private scalar 0x11..11): PEM and its raw base64url scalar.
_PEM_PRIV = (
    "-----BEGIN EC PRIVATE KEY-----\n"
    "MHcCAQEEIBERERERERERERERERERERERERERERERERERERERERERoAoGCCqGSM49\n"
    "AwEHoUQDQgAEAhfmF/C2RDkoJ4+WmZ5pojpPLBUr321s32bluAKC1O0ZSn3ry5dx\n"
    "LS3aPKhaqHZaVvRfx1hZllLyiXxlMG5XlA==\n"
    "-----END EC PRIVATE KEY-----"
)
_RAW_PRIV = "ERERERERERERERERERERERERERERERERERERERERERE"


def test_notifier_converts_pem_private_to_raw_scalar():
    import base64

    notifier = Notifier("db", "PUB", _PEM_PRIV)
    scalar = notifier._private_key
    assert not scalar.startswith("-----")
    assert len(base64.urlsafe_b64decode(scalar + "==")) == 32
    assert scalar == _RAW_PRIV


def test_notifier_passes_raw_scalar_through_unchanged():
    notifier = Notifier("db", "PUB", _RAW_PRIV)
    assert notifier._private_key == _RAW_PRIV


async def test_notifier_noops_without_vapid(tmp_path, monkeypatch):
    db = _db(tmp_path)
    push_store.upsert(db, "mdopp", "https://push/1", "p", "a")
    calls: list[str] = []
    _fake_pywebpush(monkeypatch, calls)
    notifier = Notifier(db)  # no VAPID keys
    assert notifier.enabled is False
    await notifier.push("mdopp", "t", "b", {})
    assert calls == []


async def test_notifier_sends_one_per_subscription(tmp_path, monkeypatch):
    db = _db(tmp_path)
    push_store.upsert(db, "mdopp", "https://push/1", "p", "a")
    push_store.upsert(db, "mdopp", "https://push/2", "p", "a")
    calls: list[str] = []
    _fake_pywebpush(monkeypatch, calls)
    notifier = Notifier(db, "PUB", "PRIV", "mailto:a@b")
    await notifier.push("mdopp", "Timer", "abgelaufen", {})
    assert sorted(calls) == ["https://push/1", "https://push/2"]


async def test_notifier_prunes_on_410(tmp_path, monkeypatch):
    db = _db(tmp_path)
    push_store.upsert(db, "mdopp", "https://push/gone", "p", "a")
    _fake_pywebpush(monkeypatch, [], raise_status=410)
    notifier = Notifier(db, "PUB", "PRIV")
    await notifier.push("mdopp", "t", "b", {})
    assert push_store.list_for_uid(db, "mdopp") == []


async def test_notifier_swallows_other_errors_and_keeps_sub(tmp_path, monkeypatch):
    db = _db(tmp_path)
    push_store.upsert(db, "mdopp", "https://push/1", "p", "a")
    _fake_pywebpush(monkeypatch, [], raise_status=500)
    notifier = Notifier(db, "PUB", "PRIV")
    await notifier.push("mdopp", "t", "b", {})  # must not raise
    assert len(push_store.list_for_uid(db, "mdopp")) == 1


# ---- scheduler wiring -----------------------------------------------------


class _FakeNotifier:
    def __init__(self):
        self.pushes: list[tuple] = []

    async def push(self, uid, title, body, data):
        self.pushes.append((uid, title, body, data))


async def test_fired_timer_enqueues_push(tmp_path, monkeypatch):
    db = _db(tmp_path)
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE engine_timers (id TEXT PRIMARY KEY, owner_uid TEXT, kind TEXT,"
        " label TEXT, fire_at TEXT, session_id TEXT, status TEXT DEFAULT 'pending')"
    )
    conn.execute(
        "INSERT INTO engine_timers (id, owner_uid, kind, label, fire_at, status)"
        " VALUES ('t1', 'mdopp', 'timer', 'Tee', '2000-01-01T00:00:00+00:00',"
        " 'pending')"
    )
    conn.commit()
    conn.close()

    notifier = _FakeNotifier()
    # No HA configured → _announce returns False, but the push still fires.
    sched = TimerScheduler(db, "", "", notifier=notifier)
    await sched._fire_due()

    assert len(notifier.pushes) == 1
    uid, title, body, data = notifier.pushes[0]
    assert uid == "mdopp" and body == "Tee"
    assert data["timer_id"] == "t1"


@pytest.mark.parametrize("notifier", [None])
async def test_fire_due_without_notifier_is_safe(tmp_path, notifier):
    db = _db(tmp_path)
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE engine_timers (id TEXT PRIMARY KEY, owner_uid TEXT, kind TEXT,"
        " label TEXT, fire_at TEXT, session_id TEXT, status TEXT DEFAULT 'pending')"
    )
    conn.commit()
    conn.close()
    sched = TimerScheduler(db, "", "", notifier=notifier)
    await sched._fire_due()  # must not raise


# ---- servicebay approval events → selective Web Push (#843) ----------------

_SB_FRAME = {
    "type": "new-approval",
    "id": "appr-1",
    "kind": "deploy",
    "summary": "Deploy media-stack v3",
}


def _bridge(db, bus, notifier):
    return SbApprovalEventBridge(
        "http://sb", "", "", bus, "household", notifier=notifier
    )


async def test_servicebay_event_pushes_when_no_sse_subscriber(tmp_path):
    db = _db(tmp_path)
    push_store.upsert(db, "household", "https://up/admin", "p", "a")
    bus = EventBus()  # no subscriber open for `household`
    notifier = _FakeNotifier()
    await _bridge(db, bus, notifier)._publish(_SB_FRAME)
    assert len(notifier.pushes) == 1
    uid, _title, _body, data = notifier.pushes[0]
    assert uid == "household"
    assert data["kind"] == "servicebay" and data["id"] == "appr-1"


async def test_servicebay_event_no_push_when_sse_subscriber_open(tmp_path):
    import asyncio

    db = _db(tmp_path)
    push_store.upsert(db, "household", "https://up/admin", "p", "a")
    bus = EventBus()
    notifier = _FakeNotifier()
    # An open SSE client already receives the event live → no double-notify. The
    # subscribe generator registers its queue on the first __anext__ await.
    agen = bus.subscribe("household")
    consumer = asyncio.ensure_future(agen.__anext__())
    await asyncio.sleep(0)  # let subscribe() register its queue
    assert bus.has_subscriber("household")
    await _bridge(db, bus, notifier)._publish(_SB_FRAME)
    assert notifier.pushes == []
    consumer.cancel()

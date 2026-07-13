"""Solaris BFF over ServiceBay — /napi/servicebay/* reads + approval-event bridge.

Solaris is the BFF/hub (ADR 0010, #811): the app talks only to Solaris. Solaris
re-serves ServiceBay's companion reads (`/napi/{home,approvals,services,
upgrades}`, servicebay#2252) under its OWN `/napi/servicebay/*` (device-token,
fail-closed) and republishes SB's `/napi/approvals/events` SSE
(`NewApprovalEvent`, servicebay#2268) onto the Solaris event bus so it reaches
the app over the existing `/napi/portal/events` SSE (#806).

Replays the device-token migration-0021 schema with raw SQL (a chat test must
NOT import alembic).
"""

from __future__ import annotations

import asyncio
import sqlite3

from solaris_chat import device_token_store
from solaris_chat.engine.notify import EventBus
from solaris_chat.engine.sb_events import SbApprovalEventBridge, _to_bus_event
from solaris_chat.server import build_app

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


class _FakeCompanion:
    """Stand-in for `SbCompanionClient` — the mock the routes read through."""

    def __init__(self, enabled=True, bodies=None):
        self.enabled = enabled
        self._bodies = bodies or {}
        self.calls: list[str] = []

    async def read(self, key):
        self.calls.append(key)
        return self._bodies.get(key)


def _app(tmp_path, db, companion=None):
    return build_app(
        hermes=_FakeEngine(),
        remote_user_header="Remote-User",
        default_uid="household",
        solaris_db_path=db,
        notes_dir=str(tmp_path),
        sb_companion=companion,
    )


# ---- /napi/servicebay/* reads: fail-closed device-token auth ----------------


async def test_napi_servicebay_without_bearer_is_401(aiohttp_client, tmp_path):
    db = _db(tmp_path)
    client = await aiohttp_client(_app(tmp_path, db, _FakeCompanion()))
    r = await client.get("/napi/servicebay/home")
    assert r.status == 401
    # Fail-closed: NOT the household default_uid.
    assert (await r.json()) == {"ok": False, "error": "unauthorized"}


async def test_napi_servicebay_service_key_bearer_is_401(aiohttp_client, tmp_path):
    db = _db(tmp_path)
    client = await aiohttp_client(_app(tmp_path, db, _FakeCompanion()))
    r = await client.get(
        "/napi/servicebay/approvals",
        headers={"Authorization": "Bearer SOLARIS_API_KEY"},
    )
    assert r.status == 401


async def test_napi_servicebay_remote_user_header_is_401(aiohttp_client, tmp_path):
    db = _db(tmp_path)
    client = await aiohttp_client(_app(tmp_path, db, _FakeCompanion()))
    r = await client.get("/napi/servicebay/services", headers={"Remote-User": "mdopp"})
    assert r.status == 401


# ---- /napi/servicebay/* reads: aggregate SB's body verbatim -----------------


async def test_napi_servicebay_reads_return_sb_body(aiohttp_client, tmp_path):
    db = _db(tmp_path)
    _, token = device_token_store.create(db, "mdopp")
    bodies = {
        "home": {
            "servicesUp": 5,
            "servicesFailed": 0,
            "servicesDown": 1,
            "pendingApprovals": 2,
            "pendingUpdates": 3,
        },
        "approvals": {"approvals": [{"id": "a1", "status": "pending"}]},
        "services": {"services": [{"name": "solaris-chat", "health": "healthy"}]},
        "upgrades": {
            "upgrades": [
                {
                    "name": "media",
                    "kind": "template",
                    "current": "v1",
                    "available": "v2",
                }
            ]
        },
    }
    companion = _FakeCompanion(bodies=bodies)
    client = await aiohttp_client(_app(tmp_path, db, companion))
    hdr = {"Authorization": f"Bearer {token}"}
    for key, body in bodies.items():
        r = await client.get(f"/napi/servicebay/{key}", headers=hdr)
        assert r.status == 200
        assert (await r.json()) == body
    # Each read went through the companion once, keyed by the path segment.
    assert companion.calls == ["home", "approvals", "services", "upgrades"]


async def test_napi_servicebay_unknown_key_is_404(aiohttp_client, tmp_path):
    db = _db(tmp_path)
    _, token = device_token_store.create(db, "mdopp")
    companion = _FakeCompanion()
    client = await aiohttp_client(_app(tmp_path, db, companion))
    r = await client.get(
        "/napi/servicebay/secrets", headers={"Authorization": f"Bearer {token}"}
    )
    assert r.status == 404
    # An unknown key must never reach ServiceBay.
    assert companion.calls == []


async def test_napi_servicebay_unreachable_sb_is_502(aiohttp_client, tmp_path):
    db = _db(tmp_path)
    _, token = device_token_store.create(db, "mdopp")
    # read() returns None → ServiceBay unreachable / non-200 / malformed.
    companion = _FakeCompanion(bodies={})
    client = await aiohttp_client(_app(tmp_path, db, companion))
    r = await client.get(
        "/napi/servicebay/home", headers={"Authorization": f"Bearer {token}"}
    )
    assert r.status == 502
    assert (await r.json()) == {"ok": False, "error": "servicebay_unavailable"}


async def test_napi_servicebay_unconfigured_is_503(aiohttp_client, tmp_path):
    db = _db(tmp_path)
    _, token = device_token_store.create(db, "mdopp")
    companion = _FakeCompanion(enabled=False)
    client = await aiohttp_client(_app(tmp_path, db, companion))
    r = await client.get(
        "/napi/servicebay/home", headers={"Authorization": f"Bearer {token}"}
    )
    assert r.status == 503
    assert (await r.json()) == {"ok": False, "error": "servicebay_unconfigured"}


# ---- event bridge: SB NewApprovalEvent → event bus → /napi/portal/events ----


def test_to_bus_event_maps_new_approval_and_drops_keepalives():
    ev = _to_bus_event(
        {
            "type": "new-approval",
            "id": "req-1",
            "kind": "media",
            "summary": "Enable providers",
            "created_at": "t0",
        }
    )
    assert ev == {"id": "req-1", "kind": "media", "summary": "Enable providers"}
    # Keep-alive / handshake / malformed frames produce nothing.
    assert _to_bus_event({"type": "connected"}) is None
    assert _to_bus_event({"type": "ping"}) is None
    assert _to_bus_event({"type": "new-approval"}) is None  # no id
    assert _to_bus_event({}) is None


async def test_bridge_republishes_sb_event_onto_bus(monkeypatch, tmp_path):
    """An SB new-approval frame from the SSE lands on the event bus scoped to the
    Wartung uid, so it reaches /napi/portal/events."""
    bus = EventBus()
    bridge = SbApprovalEventBridge(
        "http://sb:5888", str(tmp_path / "tok"), bus, "household"
    )

    class _FakeContent:
        def __init__(self, lines):
            self._lines = lines

        def __aiter__(self):
            async def gen():
                for line in self._lines:
                    yield line

            return gen()

    class _FakeResp:
        status = 200

        def __init__(self, lines):
            self.content = _FakeContent(lines)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    class _FakeSession:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def get(self, url, headers=None):
            return _FakeResp(
                [
                    b'data: {"type":"connected"}\n',
                    b'data: {"type":"new-approval","id":"req-9",'
                    b'"kind":"media","summary":"Enable providers","created_at":"t"}\n',
                    b'data: {"type":"ping"}\n',
                ]
            )

    monkeypatch.setattr(
        "solaris_chat.engine.sb_events.aiohttp.ClientSession", _FakeSession
    )

    # Subscribe as the app would over /napi/portal/events (the household stream).
    received: list[dict] = []

    async def _drain():
        async for event in bus.subscribe("household"):
            received.append(event)
            break

    drain = asyncio.ensure_future(_drain())
    await asyncio.sleep(0.01)
    await bridge._consume_once()
    await asyncio.wait_for(drain, 2)

    assert received == [
        {
            "kind": "servicebay",
            "data": {"id": "req-9", "kind": "media", "summary": "Enable providers"},
        }
    ]

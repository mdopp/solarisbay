"""Live status propagation — event bus + SSE /api/events + HA-WS watcher (#714).

Covers: the typed EventBus fans a `card_state` out only to the uids that
subscribed (per-resident scope); `/api/events` delivers a published event to its
owner and a second resident never sees the first's; `favorites_store.
pinned_entity_owners` maps pinned entities to their owners; the HA-WS watcher
emits `card_state` on a simulated `state_changed` (mocked WS) to the pinning
uids, reconnects with backoff after a drop, and pushes only noteworthy
transitions when no SSE client is listening. Tables are raw SQL from migration
0019 — a chat test must NOT import alembic (CI runs solaris-chat clean).
"""

from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest

from solaris_chat import favorites_store
from solaris_chat.engine import ha_watch
from solaris_chat.engine.notify import EventBus
from solaris_chat.server import build_app

_SCHEMA = """
CREATE TABLE favorites (
  id        TEXT PRIMARY KEY,
  owner_uid TEXT NOT NULL,
  kind      TEXT NOT NULL CHECK (kind IN ('action','entity','link')),
  label     TEXT NOT NULL,
  payload   TEXT NOT NULL,
  position  INTEGER NOT NULL DEFAULT 0,
  created   TEXT NOT NULL DEFAULT (datetime('now'))
);
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


# ---- EventBus fan-out ------------------------------------------------------


async def test_bus_fans_out_only_to_subscribed_uid():
    bus = EventBus()
    mdopp = bus.subscribe("mdopp")
    lena = bus.subscribe("lena")
    # Register both queues (the generator only subscribes on first advance).
    m_task = asyncio.ensure_future(mdopp.__anext__())
    l_task = asyncio.ensure_future(lena.__anext__())
    await asyncio.sleep(0)

    bus.publish("mdopp", "card_state", {"entity_id": "light.buero"})
    got = await asyncio.wait_for(m_task, 1)
    assert got == {"kind": "card_state", "data": {"entity_id": "light.buero"}}
    # Lena's queue never received mdopp's event.
    assert not l_task.done()
    l_task.cancel()


async def test_bus_has_subscriber_tracks_open_clients():
    bus = EventBus()
    assert bus.has_subscriber("mdopp") is False
    gen = bus.subscribe("mdopp")
    task = asyncio.ensure_future(gen.__anext__())
    await asyncio.sleep(0)
    assert bus.has_subscriber("mdopp") is True
    task.cancel()
    await asyncio.gather(gen.aclose(), return_exceptions=True)
    assert bus.has_subscriber("mdopp") is False


# ---- SSE /api/events -------------------------------------------------------


def _app(tmp_path, bus, db=None):
    return build_app(
        hermes=_FakeEngine(),
        remote_user_header="Remote-User",
        default_uid="household",
        solaris_db_path=db or _db(tmp_path),
        notes_dir=str(tmp_path),
        event_bus=bus,
    )


async def _read_card_state(resp) -> dict:
    """Read the first `card_state` SSE frame's JSON data."""
    event = None
    while True:
        line = (await asyncio.wait_for(resp.content.readline(), 2)).decode().strip()
        if line.startswith("event:"):
            event = line.split(":", 1)[1].strip()
        elif line.startswith("data:") and event == "card_state":
            return json.loads(line.split(":", 1)[1].strip())


async def test_sse_delivers_card_state_to_owner(aiohttp_client, tmp_path):
    bus = EventBus()
    client = await aiohttp_client(_app(tmp_path, bus))
    resp = await client.get("/api/events", headers={"Remote-User": "mdopp"})
    assert resp.status == 200
    await asyncio.sleep(0.05)  # let the subscription register
    bus.publish("mdopp", "card_state", {"entity_id": "cover.garage", "card": {"x": 1}})
    data = await _read_card_state(resp)
    assert data["entity_id"] == "cover.garage"
    resp.close()


async def test_sse_is_owner_scoped(aiohttp_client, tmp_path):
    """A second resident's stream never carries the first's card_state."""
    bus = EventBus()
    client = await aiohttp_client(_app(tmp_path, bus))
    lena = await client.get("/api/events", headers={"Remote-User": "lena"})
    await asyncio.sleep(0.05)
    bus.publish("mdopp", "card_state", {"entity_id": "light.buero", "card": {}})
    # Lena's stream must NOT yield mdopp's event.
    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(lena.content.readline(), 0.3)
    lena.close()


async def test_sse_delivers_household_pins(aiohttp_client, tmp_path):
    """A household-scoped card_state reaches any resident's open stream."""
    bus = EventBus()
    client = await aiohttp_client(_app(tmp_path, bus))
    resp = await client.get("/api/events", headers={"Remote-User": "lena"})
    await asyncio.sleep(0.05)
    bus.publish(
        favorites_store.HOUSEHOLD,
        "card_state",
        {"entity_id": "cover.haustuer", "card": {"x": 1}},
    )
    data = await _read_card_state(resp)
    assert data["entity_id"] == "cover.haustuer"
    resp.close()


# ---- favorites_store.pinned_entity_owners ----------------------------------


def test_pinned_entity_owners_maps_entities_to_owners(tmp_path):
    db = _db(tmp_path)
    favorites_store.add_favorite(
        db, "mdopp", "entity", "L", {"entity_id": "light.buero"}
    )
    favorites_store.add_favorite(
        db, "lena", "entity", "L", {"entity_id": "light.buero"}
    )
    favorites_store.add_favorite(
        db, "household", "entity", "G", {"entity_id": "cover.garage"}
    )
    favorites_store.add_favorite(
        db, "mdopp", "action", "R", {"tool": "play_radio", "args": {}}
    )
    owners = favorites_store.pinned_entity_owners(db)
    assert owners["light.buero"] == {"mdopp", "lena"}
    assert owners["cover.garage"] == {"household"}
    # An action favorite is not an entity subscription.
    assert set(owners) == {"light.buero", "cover.garage"}


def test_pinned_entity_owners_empty_without_db(tmp_path):
    assert favorites_store.pinned_entity_owners(str(tmp_path / "nope.db")) == {}


# ---- HA-WS watcher ---------------------------------------------------------


class _FakeWSMessage:
    type = None

    def __init__(self, data):
        import aiohttp

        self.type = aiohttp.WSMsgType.TEXT
        self.data = data


class _FakeWS:
    """A scripted HA websocket: yields auth_required, accepts auth, then the
    state_changed events the test queued; raising to end the connection."""

    def __init__(self, events):
        self._events = list(events)
        self.sent: list[dict] = []
        self._auth_step = 0

    async def receive_json(self):
        self._auth_step += 1
        return (
            {"type": "auth_required"} if self._auth_step == 1 else {"type": "auth_ok"}
        )

    async def send_json(self, data):
        self.sent.append(data)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._events:
            raise StopAsyncIteration
        return _FakeWSMessage(json.dumps(self._events.pop(0)))


def _state_changed(entity_id, state, attrs=None):
    return {
        "type": "event",
        "event": {
            "data": {
                "entity_id": entity_id,
                "new_state": {"state": state, "attributes": attrs or {}},
            }
        },
    }


async def test_watcher_emits_card_state_on_state_changed(tmp_path):
    db = _db(tmp_path)
    favorites_store.add_favorite(
        db, "mdopp", "entity", "L", {"entity_id": "light.buero"}
    )
    bus = EventBus()
    got: list[dict] = []
    gen = bus.subscribe("mdopp")
    task = asyncio.ensure_future(gen.__anext__())
    await asyncio.sleep(0)

    watcher = ha_watch.HaStateWatcher("http://ha", "tok", bus, db)
    ws = _FakeWS([_state_changed("light.buero", "on", {"friendly_name": "Büro"})])
    await watcher._authenticate(ws)  # noqa: SLF001 — exercise the real auth handshake
    watcher._refresh_pins()
    async for msg in ws:
        import aiohttp

        if msg.type == aiohttp.WSMsgType.TEXT:
            watcher._on_message(json.loads(msg.data))
    got.append(await asyncio.wait_for(task, 1))
    assert got[0]["kind"] == "card_state"
    assert got[0]["data"]["entity_id"] == "light.buero"
    assert got[0]["data"]["card"]["state"] == "on"


async def test_watcher_ignores_unpinned_entity(tmp_path):
    db = _db(tmp_path)  # no pins at all
    bus = EventBus()
    watcher = ha_watch.HaStateWatcher("http://ha", "tok", bus, db)
    watcher._refresh_pins()
    published: list = []
    bus.publish = lambda *a, **k: published.append(a)  # type: ignore[assignment]
    watcher._on_message(_state_changed("light.buero", "on"))
    assert published == []


def test_auth_invalid_raises():
    async def go():
        watcher = ha_watch.HaStateWatcher("http://ha", "bad", EventBus(), "x")

        class _BadAuthWS(_FakeWS):
            async def receive_json(self):
                self._auth_step += 1
                return (
                    {"type": "auth_required"}
                    if self._auth_step == 1
                    else {"type": "auth_invalid"}
                )

        with pytest.raises(RuntimeError):
            await watcher._authenticate(_BadAuthWS([]))

    asyncio.run(go())


async def test_watcher_reconnects_after_drop(monkeypatch):
    """A dropped connection is retried with capped backoff, not fatal."""
    bus = EventBus()
    watcher = ha_watch.HaStateWatcher("http://ha", "tok", bus, "x")
    calls = {"n": 0}

    async def _flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionResetError("dropped")
        watcher._task.cancel()  # stop the loop once it has retried twice

    monkeypatch.setattr(watcher, "_connect_and_watch", _flaky)
    monkeypatch.setattr(ha_watch, "_BACKOFF_START_S", 0.0)
    monkeypatch.setattr(ha_watch, "_BACKOFF_MAX_S", 0.0)
    watcher._task = asyncio.ensure_future(watcher._run())
    await asyncio.gather(watcher._task, return_exceptions=True)
    assert calls["n"] >= 3


# ---- selective web push ----------------------------------------------------


class _FakeNotifier:
    def __init__(self):
        self.pushes: list[tuple] = []

    async def push(self, uid, title, body, data):
        self.pushes.append((uid, title, body, data))


async def test_noteworthy_pushes_when_no_sse_client(tmp_path):
    db = _db(tmp_path)
    favorites_store.add_favorite(
        db, "mdopp", "entity", "G", {"entity_id": "cover.garage"}
    )
    notifier = _FakeNotifier()
    bus = EventBus()  # nobody subscribed → the app is "closed"
    watcher = ha_watch.HaStateWatcher("http://ha", "tok", bus, db, notifier=notifier)
    watcher._refresh_pins()
    watcher._on_message(
        _state_changed(
            "cover.garage",
            "open",
            {"friendly_name": "Garage", "device_class": "garage"},
        )
    )
    await asyncio.sleep(0)  # let the scheduled push task run
    assert len(notifier.pushes) == 1
    assert notifier.pushes[0][0] == "mdopp"


async def test_light_never_pushes(tmp_path):
    db = _db(tmp_path)
    favorites_store.add_favorite(
        db, "mdopp", "entity", "L", {"entity_id": "light.buero"}
    )
    notifier = _FakeNotifier()
    bus = EventBus()
    watcher = ha_watch.HaStateWatcher("http://ha", "tok", bus, db, notifier=notifier)
    watcher._refresh_pins()
    watcher._on_message(_state_changed("light.buero", "on"))
    await asyncio.sleep(0)
    assert notifier.pushes == []


async def test_noteworthy_suppressed_when_sse_client_open(tmp_path):
    db = _db(tmp_path)
    favorites_store.add_favorite(
        db, "mdopp", "entity", "G", {"entity_id": "cover.garage"}
    )
    notifier = _FakeNotifier()
    bus = EventBus()
    gen = bus.subscribe("mdopp")  # an open SSE client is listening
    task = asyncio.ensure_future(gen.__anext__())
    await asyncio.sleep(0)
    watcher = ha_watch.HaStateWatcher("http://ha", "tok", bus, db, notifier=notifier)
    watcher._refresh_pins()
    watcher._on_message(
        _state_changed(
            "cover.garage", "open", {"friendly_name": "G", "device_class": "garage"}
        )
    )
    await asyncio.sleep(0)
    assert notifier.pushes == []  # SSE already delivered it; no phone push
    task.cancel()

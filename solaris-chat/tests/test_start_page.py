"""Start-page favorites API — aggregator + CRUD + run gate (#646).

Covers the `/api/portal/start` aggregator (personal-vs-household scoping,
live-card enrichment, frequent excluded from the curated lists), the per-id
favorites CRUD, and `/api/favorites/{id}/run` (dispatch on a sensitive/unlisted
tool is refused with 403; a routine action dispatches on the gateway toolbox).
Tables are created with raw SQL copied from migration 0019 — a chat test must
NOT import alembic (CI runs solaris-chat in a clean env without it).
"""

from __future__ import annotations

import json
import sqlite3

from solaris_chat import favorites_store
from solaris_chat.engine.tools import ha
from solaris_chat.server import build_app

# The two tables migration 0019 creates, replayed locally (no alembic).
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
CREATE TABLE favorite_usage (
  owner_uid    TEXT NOT NULL,
  kind         TEXT NOT NULL,
  payload_hash TEXT NOT NULL,
  payload      TEXT NOT NULL,
  count        INTEGER NOT NULL DEFAULT 0,
  last_used    TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (owner_uid, payload_hash)
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
    """Records the one tool dispatch a favorite run makes."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def dispatch_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return json.dumps({"ok": True, "say": "erledigt"})


def _app(tmp_path, hermes=None, **kw):
    return build_app(
        hermes=hermes or _FakeEngine(),
        remote_user_header="Remote-User",
        default_uid="household",
        solaris_db_path=_db(tmp_path),
        notes_dir=str(tmp_path),
        **kw,
    )


async def test_start_scopes_personal_and_household(aiohttp_client, tmp_path):
    db = _db(tmp_path)
    favorites_store.add_favorite(
        db,
        "mdopp",
        "action",
        "Mein Radio",
        {"tool": "play_radio", "args": {"station": "NDR"}},
    )
    favorites_store.add_favorite(
        db,
        "household",
        "action",
        "Haus Radio",
        {"tool": "play_radio", "args": {"station": "WDR"}},
    )
    favorites_store.add_favorite(
        db, "anna", "action", "Annas", {"tool": "play_radio", "args": {}}
    )
    app = build_app(
        hermes=_FakeEngine(),
        remote_user_header="Remote-User",
        default_uid="household",
        solaris_db_path=db,
        notes_dir=str(tmp_path),
    )
    client = await aiohttp_client(app)
    j = await (
        await client.get("/api/portal/start", headers={"Remote-User": "mdopp"})
    ).json()
    assert [f["label"] for f in j["personal"]] == ["Mein Radio"]
    assert [f["label"] for f in j["household"]] == ["Haus Radio"]
    # Anna's private favorite is invisible to mdopp.
    assert all("Annas" not in f["label"] for f in j["personal"] + j["household"])


async def test_start_enriches_entity_with_live_card(
    aiohttp_client, tmp_path, monkeypatch
):
    async def _fake_fetch(url, token, entity_id):
        return ha.card_spec(entity_id, "on", {"friendly_name": "Bürolicht"})

    monkeypatch.setattr("solaris_chat.server.fetch_card", _fake_fetch)
    db = _db(tmp_path)
    favorites_store.add_favorite(
        db, "mdopp", "entity", "buerolicht", {"entity_id": "light.buero"}
    )
    app = build_app(
        hermes=_FakeEngine(),
        remote_user_header="Remote-User",
        default_uid="household",
        solaris_db_path=db,
        notes_dir=str(tmp_path),
        hass_url="http://ha",
        hass_token="t",
    )
    client = await aiohttp_client(app)
    j = await (
        await client.get("/api/portal/start", headers={"Remote-User": "mdopp"})
    ).json()
    card = j["personal"][0]["card"]
    assert card["domain"] == "light" and card["state"] == "on"


async def test_frequent_excluded_from_curated(aiohttp_client, tmp_path):
    db = _db(tmp_path)
    favorites_store.record_usage(db, "mdopp", "play_radio", {"station": "NDR"})
    app = build_app(
        hermes=_FakeEngine(),
        remote_user_header="Remote-User",
        default_uid="household",
        solaris_db_path=db,
        notes_dir=str(tmp_path),
    )
    client = await aiohttp_client(app)
    j = await (
        await client.get("/api/portal/start", headers={"Remote-User": "mdopp"})
    ).json()
    assert j["personal"] == [] and j["household"] == []
    assert j["frequent"] and j["frequent"][0]["kind"] == "action"


async def test_addable_groups_by_room_and_marks_state(
    aiohttp_client, tmp_path, monkeypatch
):
    """The picker aggregator (#669/#702) groups controllable actuators by room,
    marks the already-pinned one, and flags a garage cover as sensitive. The
    garage stays ADDABLE (sensitive flag, not excluded) so a guarded card can be
    pinned; scenes/scripts/automations come back as an Automationen group."""
    db = _db(tmp_path)
    favorites_store.add_favorite(
        db, "mdopp", "entity", "Bürolicht", {"entity_id": "light.buero"}
    )

    async def _fake_addable(url, token, entity_area):
        return [
            ha.card_spec("light.buero", "on", {"friendly_name": "Bürolicht"})
            | {"room": "Büro"},
            ha.card_spec("light.kueche", "off", {"friendly_name": "Küchenlicht"})
            | {"room": "Küche"},
            ha.card_spec(
                "cover.garage",
                "closed",
                {"friendly_name": "Garagentor", "device_class": "garage"},
            )
            | {"room": "Garage"},
        ]

    async def _fake_runnables(url, token):
        return [
            {"entity_id": "scene.abend", "name": "Abendszene", "domain": "scene"},
            {
                "entity_id": "automation.gute_nacht",
                "name": "Gute Nacht",
                "domain": "automation",
            },
        ]

    async def _fake_snapshot(self):
        from solaris_chat.engine.areas import AreaSnapshot

        return AreaSnapshot(rooms=[], entity_area={})

    monkeypatch.setattr("solaris_chat.server.fetch_addable_cards", _fake_addable)
    monkeypatch.setattr("solaris_chat.server.fetch_addable_runnables", _fake_runnables)
    monkeypatch.setattr(
        "solaris_chat.engine.areas.AreaRegistry.snapshot", _fake_snapshot
    )
    app = build_app(
        hermes=_FakeEngine(),
        remote_user_header="Remote-User",
        default_uid="household",
        solaris_db_path=db,
        notes_dir=str(tmp_path),
        hass_url="http://ha",
        hass_token="t",
    )
    client = await aiohttp_client(app)
    j = await (
        await client.get("/api/portal/start/addable", headers={"Remote-User": "mdopp"})
    ).json()
    rooms = {g["room"]: g["cards"] for g in j["rooms"]}
    assert rooms["Büro"][0]["pinned"] is True
    assert rooms["Küche"][0]["pinned"] is False
    # The garage is still OFFERED (present in the payload) but flagged sensitive.
    assert rooms["Garage"][0]["sensitive"] is True
    assert rooms["Garage"][0]["pinned"] is False
    assert rooms["Küche"][0]["sensitive"] is False
    # Automationen group: scenes/scripts/automations as pinnable action cards.
    autos = {a["entity_id"]: a for a in j["automations"]}
    assert autos["scene.abend"]["kind"] == "action"
    assert autos["scene.abend"]["tool"] == "ha_run_scene_script"
    assert autos["scene.abend"]["args"] == {"entity": "scene.abend"}
    assert autos["scene.abend"]["sensitive"] is False
    assert autos["automation.gute_nacht"]["pinned"] is False


async def test_addable_marks_pinned_automation(aiohttp_client, tmp_path, monkeypatch):
    """A scene/script already pinned as an ha_run_scene_script action is marked
    `pinned` in the Automationen group so it isn't offered twice (#702)."""
    db = _db(tmp_path)
    favorites_store.add_favorite(
        db,
        "mdopp",
        "action",
        "Abendszene",
        {"tool": "ha_run_scene_script", "args": {"entity": "scene.abend"}},
    )

    async def _fake_addable(url, token, entity_area):
        return []

    async def _fake_runnables(url, token):
        return [{"entity_id": "scene.abend", "name": "Abendszene", "domain": "scene"}]

    async def _fake_snapshot(self):
        from solaris_chat.engine.areas import AreaSnapshot

        return AreaSnapshot(rooms=[], entity_area={})

    monkeypatch.setattr("solaris_chat.server.fetch_addable_cards", _fake_addable)
    monkeypatch.setattr("solaris_chat.server.fetch_addable_runnables", _fake_runnables)
    monkeypatch.setattr(
        "solaris_chat.engine.areas.AreaRegistry.snapshot", _fake_snapshot
    )
    app = build_app(
        hermes=_FakeEngine(),
        remote_user_header="Remote-User",
        default_uid="household",
        solaris_db_path=db,
        notes_dir=str(tmp_path),
        hass_url="http://ha",
        hass_token="t",
    )
    client = await aiohttp_client(app)
    j = await (
        await client.get("/api/portal/start/addable", headers={"Remote-User": "mdopp"})
    ).json()
    assert j["automations"][0]["pinned"] is True


async def test_run_dispatches_scene_script_action(aiohttp_client, tmp_path):
    """A pinned Automationen card (ha_run_scene_script) dispatches on tap like
    any non-sensitive action favorite (#702)."""
    db = _db(tmp_path)
    engine = _FakeEngine()
    fav_id = favorites_store.add_favorite(
        db,
        "mdopp",
        "action",
        "Abendszene",
        {"tool": "ha_run_scene_script", "args": {"entity": "scene.abend"}},
    )
    app = build_app(
        hermes=engine,
        remote_user_header="Remote-User",
        default_uid="household",
        solaris_db_path=db,
        notes_dir=str(tmp_path),
    )
    client = await aiohttp_client(app)
    r = await client.post(
        f"/api/favorites/{fav_id}/run", headers={"Remote-User": "mdopp"}
    )
    assert r.status == 200
    assert engine.calls == [("ha_run_scene_script", {"entity": "scene.abend"})]


async def test_addable_503_without_ha(aiohttp_client, tmp_path):
    app = _app(tmp_path)  # no hass_url/hass_token
    client = await aiohttp_client(app)
    r = await client.get("/api/portal/start/addable", headers={"Remote-User": "mdopp"})
    assert r.status == 503


async def test_create_delete_reorder(aiohttp_client, tmp_path):
    db = _db(tmp_path)
    app = build_app(
        hermes=_FakeEngine(),
        remote_user_header="Remote-User",
        default_uid="household",
        solaris_db_path=db,
        notes_dir=str(tmp_path),
    )
    client = await aiohttp_client(app)
    r = await client.post(
        "/api/favorites",
        json={
            "kind": "entity",
            "label": "Sofalicht",
            "payload": {"entity_id": "light.sofa"},
        },
        headers={"Remote-User": "mdopp"},
    )
    fav_id = (await r.json())["id"]
    # Reorder.
    r = await client.put(
        f"/api/favorites/{fav_id}",
        json={"position": 4},
        headers={"Remote-User": "mdopp"},
    )
    assert r.status == 200
    assert favorites_store.list_favorites(db, "mdopp")[0]["position"] == 4
    # Delete.
    r = await client.delete(
        f"/api/favorites/{fav_id}", headers={"Remote-User": "mdopp"}
    )
    assert r.status == 200
    assert favorites_store.list_favorites(db, "mdopp") == []


async def test_create_action_rejects_unlisted_tool(aiohttp_client, tmp_path):
    app = _app(tmp_path)
    client = await aiohttp_client(app)
    r = await client.post(
        "/api/favorites",
        json={
            "kind": "action",
            "label": "x",
            "payload": {"tool": "ha_list_entities", "args": {}},
        },
        headers={"Remote-User": "mdopp"},
    )
    assert r.status == 403


async def test_create_action_accepts_scene_script(aiohttp_client, tmp_path):
    """An Automationen pick pins as an ha_run_scene_script action — a pinnable,
    non-sensitive tool, so the create is accepted (#702)."""
    db = _db(tmp_path)
    app = build_app(
        hermes=_FakeEngine(),
        remote_user_header="Remote-User",
        default_uid="household",
        solaris_db_path=db,
        notes_dir=str(tmp_path),
    )
    client = await aiohttp_client(app)
    r = await client.post(
        "/api/favorites",
        json={
            "kind": "action",
            "label": "Abendszene",
            "payload": {
                "tool": "ha_run_scene_script",
                "args": {"entity": "scene.abend"},
            },
        },
        headers={"Remote-User": "mdopp"},
    )
    assert r.status == 200


async def test_run_dispatches_routine_action(aiohttp_client, tmp_path):
    db = _db(tmp_path)
    engine = _FakeEngine()
    fav_id = favorites_store.add_favorite(
        db,
        "mdopp",
        "action",
        "Radio",
        {"tool": "play_radio", "args": {"station": "NDR"}},
    )
    app = build_app(
        hermes=engine,
        remote_user_header="Remote-User",
        default_uid="household",
        solaris_db_path=db,
        notes_dir=str(tmp_path),
    )
    client = await aiohttp_client(app)
    r = await client.post(
        f"/api/favorites/{fav_id}/run", headers={"Remote-User": "mdopp"}
    )
    assert r.status == 200
    assert engine.calls == [("play_radio", {"station": "NDR"})]
    # Usage counter bumped for the frequent list.
    assert favorites_store.top_usage(db, "mdopp")[0]["payload"]["tool"] == "play_radio"


async def test_run_403_on_sensitive_action(aiohttp_client, tmp_path):
    db = _db(tmp_path)
    engine = _FakeEngine()
    # A lock unlock is sensitive → must not dispatch from a one-tap start page.
    fav_id = favorites_store.add_favorite(
        db,
        "mdopp",
        "action",
        "Tür auf",
        {
            "tool": "ha_call_service",
            "args": {"domain": "lock", "service": "unlock", "entity_id": "lock.front"},
        },
    )
    app = build_app(
        hermes=engine,
        remote_user_header="Remote-User",
        default_uid="household",
        solaris_db_path=db,
        notes_dir=str(tmp_path),
    )
    client = await aiohttp_client(app)
    r = await client.post(
        f"/api/favorites/{fav_id}/run", headers={"Remote-User": "mdopp"}
    )
    assert r.status == 403
    assert engine.calls == []


async def test_run_403_on_unlisted_tool(aiohttp_client, tmp_path):
    db = _db(tmp_path)
    engine = _FakeEngine()
    fav_id = favorites_store.add_favorite(
        db, "mdopp", "action", "x", {"tool": "ha_list_entities", "args": {}}
    )
    app = build_app(
        hermes=engine,
        remote_user_header="Remote-User",
        default_uid="household",
        solaris_db_path=db,
        notes_dir=str(tmp_path),
    )
    client = await aiohttp_client(app)
    r = await client.post(
        f"/api/favorites/{fav_id}/run", headers={"Remote-User": "mdopp"}
    )
    assert r.status == 403
    assert engine.calls == []


# --- Frontend-contract checks for the #702 picker (real check = box-verify) ---

from solaris_chat.server import STATIC_DIR  # noqa: E402

_HTML = (STATIC_DIR / "index.html").read_text(encoding="utf-8")


def test_picker_renders_cards_inert():
    # The picker tiles render in inert mode so a tile click can never fire a
    # device action — selection only.
    assert "renderHaCard(c, false, { picker: true })" in _HTML
    assert "var inert = !!(opts && opts.picker);" in _HTML
    assert ".hc-inert, .hc-inert * { pointer-events: none; }" in _HTML


def test_picker_offers_sensitive_and_automations():
    # Sensitive tiles stay selectable (only pinned ones are disabled); the
    # Automationen group is rendered from j.automations.
    assert 'if (c.pinned) tile.classList.add("taken");' in _HTML
    assert 'ah.textContent = "Automationen";' in _HTML
    assert "j.automations" in _HTML


def test_sensitive_card_tap_confirms_and_sends_confirmed():
    # A sensitive cover asks an explicit confirm and sends confirmed=true; the
    # server re-checks the gate, so haCall must forward the flag.
    assert 'var sensitive = c.sensitive || c.device_class === "garage";' in _HTML
    assert 'haCall(card, c, "cover." + b[1], {}, sensitive);' in _HTML
    assert "confirmed: confirmed === true," in _HTML

"""Favorites store + pin_favorite tool (#645).

Covers: pin-after-action uses the recorder args verbatim; a sensitive
ha_call_service is refused; a HELD (confirm-gated) action is never recorded so it
can't be pinned; the usage counter upserts; scope defaulting (anonymous uid
`household` vs a named resident). Tables are created with raw SQL copied from
migration 0019 — a chat test must NOT import alembic (CI runs solaris-chat in a
clean env without it).
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from solaris_chat import favorites_store
from solaris_chat.engine.tools import favorites as favorites_mod
from solaris_chat.engine.tools.favorites import build_favorites_tools

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
CREATE INDEX favorites_owner_position_idx ON favorites (owner_uid, position);
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


class _FakeRecorder:
    def __init__(self, steps: list[dict]):
        self._steps = steps

    def for_session(self, session_id, since_ts):
        return [s for s in self._steps if s.get("session_id") == session_id]


class _FakeRegistry:
    def __init__(self, classes: dict[str, str] | None = None):
        self._classes = classes or {}

    async def device_class(self, entity_id):
        return self._classes.get(entity_id)


def _tools(db, recorder, registry, resolver):
    """Build pin_favorite with resolve_entity_ref stubbed to `resolver`."""
    tools = build_favorites_tools(
        db,
        lambda: _tools.uid,
        lambda: _tools.session,
        recorder,
        registry,
        "http://ha",
        "tok",
    )
    return {t.name: t for t in tools}


_tools.uid = "mdopp"
_tools.session = "sess-1"


# ---- store ----------------------------------------------------------------


def test_store_degrades_to_empty_without_table(tmp_path):
    missing = str(tmp_path / "nope.db")
    assert favorites_store.list_favorites(missing, "mdopp") == []
    assert favorites_store.top_usage(missing, "mdopp") == []


def test_add_and_list_scopes_own_plus_household(tmp_path):
    db = _db(tmp_path)
    favorites_store.add_favorite(
        db, "mdopp", "entity", "Bürolicht", {"entity_id": "light.buro"}
    )
    favorites_store.add_favorite(
        db, "household", "entity", "Flur", {"entity_id": "light.flur"}
    )
    favorites_store.add_favorite(
        db, "lena", "entity", "Lenalicht", {"entity_id": "light.lena"}
    )
    labels = {f["label"] for f in favorites_store.list_favorites(db, "mdopp")}
    assert labels == {"Bürolicht", "Flur"}  # own + household, not lena's


def test_record_usage_upserts(tmp_path):
    db = _db(tmp_path)
    args = {"domain": "light", "service": "turn_on", "entity_id": "light.buro"}
    favorites_store.record_usage(db, "mdopp", "ha_call_service", args)
    favorites_store.record_usage(db, "mdopp", "ha_call_service", args)
    top = favorites_store.top_usage(db, "mdopp")
    assert len(top) == 1
    assert top[0]["count"] == 2


def test_remove_by_entity(tmp_path):
    db = _db(tmp_path)
    favorites_store.add_favorite(
        db, "mdopp", "entity", "Büro", {"entity_id": "light.buro"}
    )
    assert favorites_store.remove_by_entity(db, "mdopp", "light.buro") == 1
    assert favorites_store.list_favorites(db, "mdopp") == []


def test_add_favorite_entity_idempotent(tmp_path):
    db = _db(tmp_path)
    first = favorites_store.add_favorite(
        db, "mdopp", "entity", "Büro", {"entity_id": "light.buro"}
    )
    again = favorites_store.add_favorite(
        db, "mdopp", "entity", "Bürolicht", {"entity_id": "light.buro"}
    )
    assert again == first  # re-pin returns the existing id
    favs = favorites_store.list_favorites(db, "mdopp")
    assert len(favs) == 1


def test_add_favorite_action_idempotent(tmp_path):
    db = _db(tmp_path)
    payload = {"tool": "play_radio", "args": {"station": "dlf"}}
    first = favorites_store.add_favorite(db, "mdopp", "action", "Radio", payload)
    # same tool+args, keys in a different order → still a duplicate
    again = favorites_store.add_favorite(
        db,
        "mdopp",
        "action",
        "Radio",
        {"args": {"station": "dlf"}, "tool": "play_radio"},
    )
    assert again == first
    assert len(favorites_store.list_favorites(db, "mdopp")) == 1


def test_add_favorite_distinct_still_added(tmp_path):
    db = _db(tmp_path)
    favorites_store.add_favorite(
        db, "mdopp", "entity", "Büro", {"entity_id": "light.buro"}
    )
    favorites_store.add_favorite(
        db, "mdopp", "entity", "Flur", {"entity_id": "light.flur"}
    )
    favorites_store.add_favorite(
        db,
        "mdopp",
        "action",
        "Radio",
        {"tool": "play_radio", "args": {"station": "dlf"}},
    )
    favorites_store.add_favorite(
        db,
        "mdopp",
        "action",
        "Musik",
        {"tool": "play_radio", "args": {"station": "byte"}},
    )
    assert len(favorites_store.list_favorites(db, "mdopp")) == 4


# ---- pin_favorite handler -------------------------------------------------


@pytest.mark.asyncio
async def test_pin_target_creates_entity_favorite(tmp_path, monkeypatch):
    db = _db(tmp_path)

    async def resolver(url, token, ref):
        return "light.buro" if "büro" in ref.lower() else ""

    monkeypatch.setattr(favorites_mod, "resolve_entity_ref", resolver)
    _tools.uid, _tools.session = "mdopp", "sess-1"
    pin = _tools(db, _FakeRecorder([]), _FakeRegistry(), resolver)["pin_favorite"]

    out = json.loads(await pin.handler({"target": "Bürolicht"}))
    assert out["ok"] is True
    favs = favorites_store.list_favorites(db, "mdopp")
    assert favs[0]["kind"] == "entity"
    assert favs[0]["payload"] == {"entity_id": "light.buro"}


@pytest.mark.asyncio
async def test_pin_last_action_uses_recorder_args_verbatim(tmp_path, monkeypatch):
    db = _db(tmp_path)
    monkeypatch.setattr(favorites_mod, "resolve_entity_ref", _noop_resolver)
    exact = {"query": "Bohemian Rhapsody", "room": "wohnzimmer"}
    recorder = _FakeRecorder(
        [
            {
                "session_id": "sess-1",
                "step_kind": "tool",
                "tool_name": "play_music",
                "arguments": exact,
            },
        ]
    )
    _tools.uid, _tools.session = "mdopp", "sess-1"
    pin = _tools(db, recorder, _FakeRegistry(), None)["pin_favorite"]

    out = json.loads(await pin.handler({}))
    assert out["ok"] is True
    fav = favorites_store.list_favorites(db, "mdopp")[0]
    assert fav["kind"] == "action"
    assert fav["payload"] == {"tool": "play_music", "args": exact}


@pytest.mark.asyncio
async def test_pin_sensitive_action_refused(tmp_path, monkeypatch):
    db = _db(tmp_path)
    monkeypatch.setattr(favorites_mod, "resolve_entity_ref", _noop_resolver)
    recorder = _FakeRecorder(
        [
            {
                "session_id": "sess-1",
                "step_kind": "tool",
                "tool_name": "ha_call_service",
                "arguments": {
                    "domain": "lock",
                    "service": "unlock",
                    "entity_id": "lock.tur",
                },
            }
        ]
    )
    _tools.uid, _tools.session = "mdopp", "sess-1"
    pin = _tools(db, recorder, _FakeRegistry(), None)["pin_favorite"]

    out = json.loads(await pin.handler({}))
    assert out["ok"] is False
    assert out["reason"] == "confirm_gated"
    assert favorites_store.list_favorites(db, "mdopp") == []


@pytest.mark.asyncio
async def test_held_gated_action_not_pinnable(tmp_path, monkeypatch):
    """A held confirm-gated call is never record_tool'd, so the recorder has no
    step for it → pin finds no recent action, honest no_recent_action."""
    db = _db(tmp_path)
    monkeypatch.setattr(favorites_mod, "resolve_entity_ref", _noop_resolver)
    recorder = _FakeRecorder([])  # nothing recorded — the held call left no step
    _tools.uid, _tools.session = "mdopp", "sess-1"
    pin = _tools(db, recorder, _FakeRegistry(), None)["pin_favorite"]

    out = json.loads(await pin.handler({}))
    assert out["ok"] is False
    assert out["reason"] == "no_recent_action"


@pytest.mark.asyncio
async def test_older_record_without_arguments_skipped(tmp_path, monkeypatch):
    db = _db(tmp_path)
    monkeypatch.setattr(favorites_mod, "resolve_entity_ref", _noop_resolver)
    recorder = _FakeRecorder(
        [
            {
                "session_id": "sess-1",
                "step_kind": "tool",
                "tool_name": "play_music",
                "arguments": None,
            }
        ]
    )
    _tools.uid, _tools.session = "mdopp", "sess-1"
    pin = _tools(db, recorder, _FakeRegistry(), None)["pin_favorite"]

    out = json.loads(await pin.handler({}))
    assert out["reason"] == "no_recent_action"


@pytest.mark.asyncio
async def test_anonymous_uid_pins_to_household(tmp_path, monkeypatch):
    db = _db(tmp_path)

    async def resolver(url, token, ref):
        return "light.flur"

    monkeypatch.setattr(favorites_mod, "resolve_entity_ref", resolver)
    _tools.uid, _tools.session = "household", "sess-1"
    pin = _tools(db, _FakeRecorder([]), _FakeRegistry(), resolver)["pin_favorite"]

    await pin.handler({"target": "Flur"})
    assert (
        favorites_store.list_favorites(db, "household")[0]["owner_uid"] == "household"
    )
    # A named resident's list is empty — the anonymous pin landed on household.
    assert favorites_store.list_favorites(db, "lena")[0]["owner_uid"] == "household"


@pytest.mark.asyncio
async def test_scope_household_forces_shared_owner(tmp_path, monkeypatch):
    db = _db(tmp_path)

    async def resolver(url, token, ref):
        return "light.flur"

    monkeypatch.setattr(favorites_mod, "resolve_entity_ref", resolver)
    _tools.uid, _tools.session = "mdopp", "sess-1"
    pin = _tools(db, _FakeRecorder([]), _FakeRegistry(), resolver)["pin_favorite"]

    await pin.handler({"target": "Flur", "scope": "household"})
    assert favorites_store.list_favorites(db, "mdopp")[0]["owner_uid"] == "household"


async def _noop_resolver(url, token, ref):
    return ""


# --- _favorite_label: readable names + actions in "Häufig genutzt" (#741) ---

from solaris_chat.server import _favorite_label, _service_label  # noqa: E402


def test_favorite_label_resolves_friendly_name_and_action():
    payload = {
        "tool": "ha_call_service",
        "args": {"entity_id": "light.dimmer_2", "service": "turn_off"},
    }
    names = {"light.dimmer_2": "Bürolicht"}
    assert _favorite_label(payload, names) == "Bürolicht — Aus"


def test_favorite_label_without_map_humanizes_slug_no_crash():
    payload = {
        "tool": "ha_call_service",
        "args": {"entity_id": "light.dimmer_2", "service": "turn_on"},
    }
    assert _favorite_label(payload) == "Dimmer 2 — An"


def test_service_label_maps_common_services():
    assert _service_label("turn_off") == "Aus"
    assert _service_label("turn_on") == "An"
    assert _service_label("toggle") == "Umschalten"
    assert _service_label("open_cover") == "Öffnen"
    assert _service_label("close_cover") == "Schließen"
    assert _service_label("stop_cover") == "Stopp"


def test_service_label_humanizes_unmapped_service():
    assert _service_label("set_fan_speed") == "Set Fan Speed"


def test_favorite_label_known_non_ha_tool():
    assert _favorite_label({"tool": "play_radio", "args": {}}) == "Radio abspielen"


def test_favorite_label_unknown_tool_humanizes():
    assert (
        _favorite_label({"tool": "some_custom_tool", "args": {}}) == "Some Custom Tool"
    )


def test_favorite_label_falls_back_to_arg():
    assert _favorite_label({"tool": "search", "args": {"query": "Pizza"}}) == "Pizza"

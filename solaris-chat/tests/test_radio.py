"""play_radio tool (#u94): per-user favorite station, ask + store if unknown.

Security-critical: the favorite is a per-resident note under
`users/<uid>/preferences/radio-favorit.md`, read and written only for the
calling resident — caller B never sees resident A's favorite. The station is
resolved via radio-browser.info (faked here) and cast through the same scoped
`media_player.play_media` path play_music uses; `played:true` only on a
confirmed HA ok.
"""

from __future__ import annotations

import json
from pathlib import Path

from solaris_chat.engine.tools import radio as radio_mod
from solaris_chat.engine.tools.radio import (
    _read_favorite,
    _write_favorite,
    build_radio_tools,
)


class _FakeResolver:
    """Resolve a fixed mapping name->(canonical, url); everything else None."""

    def __init__(self, mapping):
        self._mapping = mapping
        self.calls: list[str] = []

    async def resolve_station(self, name):
        self.calls.append(name)
        return self._mapping.get(name)


def _stub(monkeypatch, mapping, *, ok=True):
    """Inject a fake resolver into the tool and record HA play_media calls."""
    resolver = _FakeResolver(mapping)
    monkeypatch.setattr(radio_mod, "RadioBrowserClient", lambda *a, **k: resolver)
    calls: list[tuple] = []

    async def _fake_cast(hass_url, hass_token, entity_id, service, data):
        calls.append((hass_url, hass_token, entity_id, service, data))
        return {"ok": ok, "state": "playing" if ok else None}

    monkeypatch.setattr(radio_mod, "call_service_scoped", _fake_cast)
    return resolver, calls


def _tool(notes_dir, uid):
    (play,) = build_radio_tools(notes_dir, "http://ha", "tok", lambda: uid)
    return play


async def _call(notes_dir, uid, args, monkeypatch, mapping, *, ok=True):
    resolver, calls = _stub(monkeypatch, mapping, ok=ok)
    out = json.loads(await _tool(notes_dir, uid).handler(args))
    return out, resolver, calls


async def test_station_resolves_writes_favorite_and_casts(tmp_path, monkeypatch):
    notes = str(tmp_path)
    out, resolver, calls = await _call(
        notes,
        "mdopp",
        {"station": "WDR 2", "entity_id": "media_player.kuche"},
        monkeypatch,
        {"WDR 2": ("WDR 2", "http://stream/wdr2")},
    )
    assert out == {
        "ok": True,
        "station": "WDR 2",
        "entity_id": "media_player.kuche",
        "played": True,
    }
    # Favorite stored under the caller's own users/<uid>/ space.
    fav = Path(notes) / "users" / "mdopp" / "preferences" / "radio-favorit.md"
    assert fav.is_file()
    assert _read_favorite(notes, "mdopp") == {
        "name": "WDR 2",
        "stream_url": "http://stream/wdr2",
    }
    # Cast is media_player.play_media with content_type=music + the resolved URL.
    (_, _, entity_id, service, data) = calls[0]
    assert service == "media_player.play_media"
    assert entity_id == "media_player.kuche"
    assert data == {
        "media_content_type": "music",
        "media_content_id": "http://stream/wdr2",
    }


async def test_no_station_plays_stored_favorite(tmp_path, monkeypatch):
    notes = str(tmp_path)
    _write_favorite(notes, "mdopp", "WDR 2", "http://stream/wdr2")
    # No resolver hit needed — the favorite is read back deterministically.
    out, resolver, calls = await _call(
        notes,
        "mdopp",
        {"entity_id": "media_player.kuche"},
        monkeypatch,
        {},
    )
    assert out == {
        "ok": True,
        "station": "WDR 2",
        "entity_id": "media_player.kuche",
        "played": True,
    }
    assert resolver.calls == []  # favorite read, not searched
    assert calls[0][4]["media_content_id"] == "http://stream/wdr2"


async def test_no_favorite_no_station_does_not_cast(tmp_path, monkeypatch):
    out, _, calls = await _call(
        str(tmp_path),
        "mdopp",
        {"entity_id": "media_player.kuche"},
        monkeypatch,
        {},
    )
    assert out == {"ok": False, "reason": "no_favorite"}
    assert calls == []


async def test_favorite_is_per_user(tmp_path, monkeypatch):
    notes = str(tmp_path)
    # Resident A stores a favorite.
    _write_favorite(notes, "alice", "WDR 2", "http://stream/wdr2")
    # Caller B (no favorite of their own) must NOT read A's favorite.
    out, _, calls = await _call(
        notes,
        "bob",
        {"entity_id": "media_player.kuche"},
        monkeypatch,
        {},
    )
    assert out == {"ok": False, "reason": "no_favorite"}
    assert calls == []


async def test_station_not_found_does_not_cast(tmp_path, monkeypatch):
    out, _, calls = await _call(
        str(tmp_path),
        "mdopp",
        {"station": "Nonsense FM", "entity_id": "media_player.kuche"},
        monkeypatch,
        {},  # resolver returns None
    )
    assert out == {
        "ok": False,
        "reason": "station_not_found",
        "query": "Nonsense FM",
    }
    assert calls == []


async def test_need_device_when_no_entity(tmp_path, monkeypatch):
    out, _, calls = await _call(
        str(tmp_path),
        "mdopp",
        {"station": "WDR 2"},
        monkeypatch,
        {"WDR 2": ("WDR 2", "http://stream/wdr2")},
    )
    assert out == {"ok": False, "reason": "need_device"}
    assert calls == []


# -- u99: device-less radio defaults to the originating room's media_player ---


def _tool_with_room(notes_dir, uid, *, room, resolver):
    (play,) = build_radio_tools(
        notes_dir,
        "http://ha",
        "tok",
        lambda: uid,
        room_getter=lambda: room,
        room_resolver=resolver,
    )
    return play


async def test_radio_defaults_to_current_room(tmp_path, monkeypatch):
    notes = str(tmp_path)
    _, calls = _stub(monkeypatch, {"WDR 2": ("WDR 2", "http://stream/wdr2")})

    async def _resolver(room):
        return "media_player.kuche" if room == "Küche" else None

    play = _tool_with_room(notes, "mdopp", room="Küche", resolver=_resolver)
    out = json.loads(await play.handler({"station": "WDR 2"}))
    # No entity_id named, but a current room is known → cast there.
    assert out["ok"] is True
    assert out["entity_id"] == "media_player.kuche"
    assert calls[0][2] == "media_player.kuche"


async def test_radio_no_room_no_device_need_device(tmp_path, monkeypatch):
    notes = str(tmp_path)
    _, calls = _stub(monkeypatch, {"WDR 2": ("WDR 2", "http://stream/wdr2")})

    async def _resolver(room):
        return None

    play = _tool_with_room(notes, "mdopp", room="", resolver=_resolver)
    out = json.loads(await play.handler({"station": "WDR 2"}))
    assert out == {"ok": False, "reason": "need_device"}
    assert calls == []


async def test_play_failed_never_played(tmp_path, monkeypatch):
    notes = str(tmp_path)
    out, _, calls = await _call(
        notes,
        "mdopp",
        {"station": "WDR 2", "entity_id": "media_player.wohnzimmer"},
        monkeypatch,
        {"WDR 2": ("WDR 2", "http://stream/wdr2")},
        ok=False,
    )
    assert out["ok"] is False
    assert out["reason"] == "play_failed"
    assert out["station"] == "WDR 2"
    # Favorite still stored (resolution succeeded) but it never played.
    assert _read_favorite(notes, "mdopp")["name"] == "WDR 2"
    assert "played" not in out


async def test_favorite_note_roundtrips_name_and_url(tmp_path):
    notes = str(tmp_path)
    _write_favorite(notes, "mdopp", "Radio Bob", "http://stream/bob?x=1")
    assert _read_favorite(notes, "mdopp") == {
        "name": "Radio Bob",
        "stream_url": "http://stream/bob?x=1",
    }
    # Absent favorite reads back as None.
    assert _read_favorite(notes, "someone-else") is None


def test_write_favorite_sanitizes_newline_injection(tmp_path):
    notes = str(tmp_path)
    # A station name carrying a newline must not inject extra frontmatter lines.
    _write_favorite(
        notes,
        "mdopp",
        "Evil\nstream_url: http://attacker/owned",
        "http://stream/wdr2\nname: spoofed",
    )
    fav = Path(notes) / "users" / "mdopp" / "preferences" / "radio-favorit.md"
    text = fav.read_text(encoding="utf-8")
    # Exactly one name + one stream_url field; no injected lines.
    assert text.count("\nname:") == 1
    assert text.count("\nstream_url:") == 1
    assert _read_favorite(notes, "mdopp") == {
        "name": "Evilstream_url: http://attacker/owned",
        "stream_url": "http://stream/wdr2name: spoofed",
    }

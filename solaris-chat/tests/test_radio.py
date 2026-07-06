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
    _read_default_device,
    _read_favorite,
    _write_default_device,
    _write_favorite,
    build_radio_tools,
    is_cast_500,
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
    assert out == {
        "ok": False,
        "reason": "no_favorite",
        "say": "Welcher ist dein Lieblingssender?",
    }
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
    assert out == {
        "ok": False,
        "reason": "no_favorite",
        "say": "Welcher ist dein Lieblingssender?",
    }
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


async def test_need_default_device_when_no_entity_no_room(tmp_path, monkeypatch):
    # No device, no room, no stored default → ask for the default device (#622).
    out, _, calls = await _call(
        str(tmp_path),
        "mdopp",
        {"station": "WDR 2"},
        monkeypatch,
        {"WDR 2": ("WDR 2", "http://stream/wdr2")},
    )
    assert out == {
        "ok": False,
        "reason": "need_default_device",
        "say": "Auf welchem Gerät soll ich standardmäßig spielen?",
    }
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


async def test_radio_no_room_no_device_need_default_device(tmp_path, monkeypatch):
    notes = str(tmp_path)
    _, calls = _stub(monkeypatch, {"WDR 2": ("WDR 2", "http://stream/wdr2")})

    async def _resolver(room):
        return None

    play = _tool_with_room(notes, "mdopp", room="", resolver=_resolver)
    out = json.loads(await play.handler({"station": "WDR 2"}))
    assert out == {
        "ok": False,
        "reason": "need_default_device",
        "say": "Auf welchem Gerät soll ich standardmäßig spielen?",
    }
    assert calls == []


async def test_dialog_say_lines_end_in_question_mark(tmp_path, monkeypatch):
    # The say line steers the model verbatim (#404) and MUST end in `?` so the
    # facade re-opens the mic (concept §1.1). no_favorite + need_default_device.
    no_fav, _, _ = await _call(
        str(tmp_path), "alice", {"entity_id": "media_player.kuche"}, monkeypatch, {}
    )
    assert no_fav["reason"] == "no_favorite"
    assert no_fav["say"].rstrip().endswith("?")

    need_dev, _, _ = await _call(
        str(tmp_path),
        "bob",
        {"station": "WDR 2"},
        monkeypatch,
        {"WDR 2": ("WDR 2", "http://stream/wdr2")},
    )
    assert need_dev["reason"] == "need_default_device"
    assert need_dev["say"].rstrip().endswith("?")


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


# -- u103 (#622): learned per-user default playback device -------------------


async def test_radio_explicit_device_stores_default(tmp_path, monkeypatch):
    notes = str(tmp_path)
    # First explicit device with no default yet → casts there AND stores it.
    out, _, calls = await _call(
        notes,
        "mdopp",
        {"station": "WDR 2", "entity_id": "media_player.kuche"},
        monkeypatch,
        {"WDR 2": ("WDR 2", "http://stream/wdr2")},
    )
    assert out["ok"] is True and out["entity_id"] == "media_player.kuche"
    assert calls[0][2] == "media_player.kuche"
    assert _read_default_device(notes, "mdopp") == "media_player.kuche"


async def test_radio_deviceless_reuses_stored_default(tmp_path, monkeypatch):
    notes = str(tmp_path)
    _write_default_device(notes, "mdopp", "media_player.bad")
    _write_favorite(notes, "mdopp", "WDR 2", "http://stream/wdr2")
    # No device, no room, but a stored default → cast there (no need_default_device).
    out, _, calls = await _call(notes, "mdopp", {}, monkeypatch, {})
    assert out["ok"] is True
    assert out["entity_id"] == "media_player.bad"
    assert calls[0][2] == "media_player.bad"


async def test_radio_explicit_device_oneoff_keeps_stored_default(tmp_path, monkeypatch):
    notes = str(tmp_path)
    _write_default_device(notes, "mdopp", "media_player.bad")
    # An explicit device when a default already exists is a one-off; default stays.
    out, _, calls = await _call(
        notes,
        "mdopp",
        {"station": "WDR 2", "entity_id": "media_player.kuche"},
        monkeypatch,
        {"WDR 2": ("WDR 2", "http://stream/wdr2")},
    )
    assert out["entity_id"] == "media_player.kuche"
    assert calls[0][2] == "media_player.kuche"
    assert _read_default_device(notes, "mdopp") == "media_player.bad"


async def test_radio_room_wins_over_stored_default(tmp_path, monkeypatch):
    notes = str(tmp_path)
    _write_default_device(notes, "mdopp", "media_player.bad")
    _, calls = _stub(monkeypatch, {"WDR 2": ("WDR 2", "http://stream/wdr2")})

    async def _resolver(room):
        return "media_player.kuche" if room == "Küche" else None

    play = _tool_with_room(notes, "mdopp", room="Küche", resolver=_resolver)
    out = json.loads(await play.handler({"station": "WDR 2"}))
    # Current room takes precedence over the stored default.
    assert out["entity_id"] == "media_player.kuche"
    assert calls[0][2] == "media_player.kuche"
    assert _read_default_device(notes, "mdopp") == "media_player.bad"


async def test_radio_default_device_is_per_user(tmp_path, monkeypatch):
    notes = str(tmp_path)
    _write_default_device(notes, "alice", "media_player.alice")
    # Caller B has no default of their own and must NOT read A's.
    out, _, calls = await _call(notes, "bob", {}, monkeypatch, {})
    assert out == {
        "ok": False,
        "reason": "need_default_device",
        "say": "Auf welchem Gerät soll ich standardmäßig spielen?",
    }
    assert calls == []
    assert _read_default_device(notes, "bob") is None


def test_default_device_note_roundtrips(tmp_path):
    notes = str(tmp_path)
    assert _read_default_device(notes, "mdopp") is None
    _write_default_device(notes, "mdopp", "media_player.kuche")
    assert _read_default_device(notes, "mdopp") == "media_player.kuche"
    fav = Path(notes) / "users" / "mdopp" / "preferences" / "default-device.md"
    assert fav.is_file()


# -- group-cast fallback (#638): a 500 on a Cast group → a same-area device ----


def test_is_cast_500_only_5xx():
    # 5xx (group rejects URL) → fallback; ok / 4xx / transport error → not.
    assert is_cast_500({"ok": False, "error": "HA 500: Internal Server Error"})
    assert is_cast_500({"ok": False, "error": "HA 503: down"})
    assert not is_cast_500({"ok": True, "state": "playing"})
    assert not is_cast_500({"ok": False, "error": "HA 404: not found"})
    assert not is_cast_500({"ok": False, "error": "invalid entity_id"})


def _stub_seq(monkeypatch, results):
    """call_service_scoped returning a queued sequence, recording (entity_id,…)."""
    monkeypatch.setattr(radio_mod, "RadioBrowserClient", lambda *a, **k: None)
    calls: list[tuple] = []
    seq = list(results)

    async def _fake_cast(hass_url, hass_token, entity_id, service, data):
        calls.append((entity_id, data))
        return seq[min(len(calls) - 1, len(seq) - 1)]

    monkeypatch.setattr(radio_mod, "call_service_scoped", _fake_cast)
    return calls


def _tool_with_fallback(notes_dir, uid, fallbacks):
    async def _area_fallback(entity_id):
        return list(fallbacks)

    (play,) = build_radio_tools(
        notes_dir,
        "http://ha",
        "tok",
        lambda: uid,
        area_fallback=_area_fallback,
    )
    return play


async def test_radio_group_500_falls_back_to_same_area_device(tmp_path, monkeypatch):
    notes = str(tmp_path)
    _write_favorite(notes, "mdopp", "WDR 2", "http://stream/wdr2")
    # The Cast group 500s; the same-area Voice PE plays.
    calls = _stub_seq(
        monkeypatch,
        [
            {"ok": False, "error": "HA 500: group reject"},
            {"ok": True, "state": "playing"},
        ],
    )
    play = _tool_with_fallback(
        notes, "mdopp", ["media_player.home_assistant_voice_0907c9_media_player"]
    )
    out = json.loads(await play.handler({"entity_id": "media_player.wohnzimmer"}))
    assert out["ok"] is True and out["played"] is True
    # Reports the device that ACTUALLY played, not the failed group.
    assert out["entity_id"] == "media_player.home_assistant_voice_0907c9_media_player"
    assert calls[0][0] == "media_player.wohnzimmer"
    assert calls[1][0] == "media_player.home_assistant_voice_0907c9_media_player"


async def test_radio_group_500_no_candidate_returns_original(tmp_path, monkeypatch):
    notes = str(tmp_path)
    _write_favorite(notes, "mdopp", "WDR 2", "http://stream/wdr2")
    calls = _stub_seq(monkeypatch, [{"ok": False, "error": "HA 500: group reject"}])
    play = _tool_with_fallback(notes, "mdopp", [])  # no same-area device
    out = json.loads(await play.handler({"entity_id": "media_player.wohnzimmer"}))
    # Honest failure — no fake success, no extra cast.
    assert out["ok"] is False and out["reason"] == "play_failed"
    assert out["detail"] == "HA 500: group reject"
    assert len(calls) == 1


async def test_radio_non_500_failure_no_fallback(tmp_path, monkeypatch):
    notes = str(tmp_path)
    _write_favorite(notes, "mdopp", "WDR 2", "http://stream/wdr2")
    # A 404 (not a 500-class) must NOT trigger the same-area fallback.
    calls = _stub_seq(monkeypatch, [{"ok": False, "error": "HA 404: not found"}])
    play = _tool_with_fallback(
        notes, "mdopp", ["media_player.home_assistant_voice_0907c9_media_player"]
    )
    out = json.loads(await play.handler({"entity_id": "media_player.wohnzimmer"}))
    assert out["ok"] is False and out["reason"] == "play_failed"
    assert len(calls) == 1  # only the original target, no fallback cast


async def test_radio_single_device_success_no_fallback(tmp_path, monkeypatch):
    notes = str(tmp_path)
    _write_favorite(notes, "mdopp", "WDR 2", "http://stream/wdr2")
    calls = _stub_seq(monkeypatch, [{"ok": True, "state": "playing"}])
    play = _tool_with_fallback(
        notes, "mdopp", ["media_player.home_assistant_voice_0907c9_media_player"]
    )
    out = json.loads(await play.handler({"entity_id": "media_player.kuche"}))
    assert out["ok"] is True and out["entity_id"] == "media_player.kuche"
    assert len(calls) == 1  # the Küche single device plays directly, no fallback


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

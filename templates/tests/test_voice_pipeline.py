"""Tests for the Phase-2 HA voice-pipeline wiring in the post-deploy.

wire_voice_pipeline registers wyoming whisper/piper, creates the ollama-
integration conversation agent against the engine facade and builds the
"Sol" Assist pipeline. The HA REST helpers are monkeypatched with canned
responses; the websocket path is covered by patching HAWebSocket."""

from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

TEMPLATES = pathlib.Path(__file__).resolve().parents[1]


def _load(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def pd():
    return _load("solilos_pd_voice", TEMPLATES / "solilos" / "post-deploy.py")


def test_flow_create_happy_path(pd, monkeypatch):
    posts = []

    def fake_post(path, token, payload, timeout=30.0):
        posts.append((path, payload))
        if path == "/api/config/config_entries/flow":
            return 200, {"flow_id": "f1", "type": "form"}
        return 200, {"type": "create_entry", "result": {"entry_id": "e1"}}

    monkeypatch.setattr(pd, "_ha_post", fake_post)
    state, result = pd._flow_create("tok", "wyoming", [{"host": "h", "port": 1}])
    assert state == "created"
    assert result == {"entry_id": "e1"}
    assert posts[0][1] == {"handler": "wyoming"}
    assert posts[1][1] == {"host": "h", "port": 1}


def test_flow_create_already_configured_aborts_ok(pd, monkeypatch):
    def fake_post(path, token, payload, timeout=30.0):
        if path == "/api/config/config_entries/flow":
            return 200, {"flow_id": "f1", "type": "form"}
        return 200, {"type": "abort", "reason": "already_configured"}

    monkeypatch.setattr(pd, "_ha_post", fake_post)
    state, _ = pd._flow_create("tok", "wyoming", [{"host": "h", "port": 1}])
    assert state == "already"


def test_flow_create_failure_aborts_dangling_flow(pd, monkeypatch):
    deleted = []

    def fake_post(path, token, payload, timeout=30.0):
        if path == "/api/config/config_entries/flow":
            return 200, {"flow_id": "f1", "type": "form"}
        return 200, {"flow_id": "f1", "type": "form", "errors": {"base": "x"}}

    monkeypatch.setattr(pd, "_ha_post", fake_post)
    monkeypatch.setattr(
        pd, "_ha_request_delete", lambda path, token, timeout=10.0: deleted.append(path)
    )
    state, _ = pd._flow_create("tok", "wyoming", [{"host": "h", "port": 1}])
    assert state == "failed"
    assert deleted and "f1" in deleted[0]


def test_conversation_agent_creates_entry_and_subentry(pd, monkeypatch):
    subentry_posts = []

    def fake_post(path, token, payload, timeout=30.0):
        if path == "/api/config/config_entries/flow":
            return 200, {"flow_id": "f1", "type": "form"}
        if path == "/api/config/config_entries/flow/f1":
            return 200, {"type": "create_entry", "result": {"entry_id": "oll1"}}
        if path == "/api/config/config_entries/subentries/flow":
            subentry_posts.append(payload)
            return 200, {"flow_id": "s1", "type": "form"}
        if path == "/api/config/config_entries/subentries/flow/s1":
            subentry_posts.append(payload)
            return 200, {"type": "create_entry"}
        raise AssertionError(f"unexpected POST {path}")

    entities = iter(["", "conversation.sol"])

    monkeypatch.setattr(pd, "_ha_post", fake_post)
    monkeypatch.setattr(
        pd, "_find_entity", lambda token, prefix, needle="": next(entities, "")
    )
    monkeypatch.setattr(pd.time, "sleep", lambda s: None)
    entity = pd.ensure_conversation_agent("tok", "8787", "key")
    assert entity == "conversation.sol"
    assert subentry_posts[0] == {"handler": ["oll1", "conversation"]}
    assert subentry_posts[1]["model"] == pd.ENGINE_MODEL
    assert subentry_posts[1]["name"] == pd.CONVERSATION_AGENT_NAME


def test_conversation_agent_idempotent_when_entity_exists(pd, monkeypatch):
    def fake_post(path, token, payload, timeout=30.0):
        if path == "/api/config/config_entries/flow":
            return 200, {"flow_id": "f1", "type": "form"}
        if path == "/api/config/config_entries/flow/f1":
            return 200, {"type": "abort", "reason": "already_configured"}
        raise AssertionError(f"unexpected POST {path}")

    monkeypatch.setattr(pd, "_ha_post", fake_post)
    monkeypatch.setattr(pd, "_ollama_entry_id", lambda token, url: "oll1")
    monkeypatch.setattr(
        pd, "_find_entity", lambda token, prefix, needle="": "conversation.sol"
    )
    assert pd.ensure_conversation_agent("tok", "8787", "key") == "conversation.sol"


class _FakeWS:
    created: list[dict] = []
    preferred: list[str] = []
    updated: list[dict] = []
    pipelines: list[dict] = []

    def __init__(self, token):
        pass

    def cmd(self, payload):
        t = payload["type"]
        if t == "assist_pipeline/pipeline/list":
            return {"pipelines": list(self.pipelines)}
        if t == "assist_pipeline/pipeline/create":
            _FakeWS.created.append(payload)
            return {"id": "p1"}
        if t == "assist_pipeline/pipeline/set_preferred":
            _FakeWS.preferred.append(payload["pipeline_id"])
            return {}
        if t == "assist_pipeline/pipeline/update":
            _FakeWS.updated.append(payload)
            return payload
        raise AssertionError(f"unexpected ws cmd {t}")

    def close(self):
        pass


def test_pipeline_created_and_preferred(pd, monkeypatch):
    _FakeWS.created, _FakeWS.preferred, _FakeWS.pipelines = [], [], []
    entity_map = {"stt.": "stt.faster_whisper", "tts.": "tts.piper"}
    monkeypatch.setattr(
        pd,
        "_find_entity",
        lambda token, prefix, needle="": entity_map.get(prefix, ""),
    )
    monkeypatch.setattr(pd, "HAWebSocket", _FakeWS)
    assigned = []
    monkeypatch.setattr(pd, "_assign_pe_pipeline", lambda token: assigned.append(1))
    assert pd.ensure_assist_pipeline("tok", "conversation.sol") is True
    create = _FakeWS.created[0]
    assert create["name"] == "Sol"
    assert create["conversation_engine"] == "conversation.sol"
    assert create["stt_engine"] == "stt.faster_whisper"
    assert create["tts_engine"] == "tts.piper"
    assert create["language"] == "de"
    assert _FakeWS.preferred == ["p1"]
    assert assigned


def test_pipeline_prefers_martin_bridge(pd, monkeypatch):
    # GPU boxes carry the wyoming_openai bridge (servicebay#1815): the
    # pipeline rides tts.openai_streaming with plain `de` + voice `kokoro`.
    _FakeWS.created, _FakeWS.preferred, _FakeWS.pipelines = [], [], []

    def find(token, prefix, needle=""):
        if prefix == "stt.":
            return "stt.faster_whisper"
        if prefix == "tts." and needle == "openai":
            return "tts.openai_streaming"
        if prefix == "tts.":
            return "tts.piper"
        return ""

    monkeypatch.setattr(pd, "_find_entity", find)
    monkeypatch.setattr(pd, "HAWebSocket", _FakeWS)
    monkeypatch.setattr(pd, "_assign_pe_pipeline", lambda token: None)
    assert pd.ensure_assist_pipeline("tok", "conversation.sol") is True
    create = _FakeWS.created[0]
    assert create["tts_engine"] == "tts.openai_streaming"
    assert create["tts_language"] == "de"
    assert create["tts_voice"] == "kokoro"


def test_pipeline_piper_fallback_keeps_regional_language(pd, monkeypatch):
    _FakeWS.created, _FakeWS.preferred, _FakeWS.pipelines = [], [], []

    def find(token, prefix, needle=""):
        if prefix == "stt.":
            return "stt.faster_whisper"
        if prefix == "tts." and needle == "openai":
            return ""
        if prefix == "tts.":
            return "tts.piper"
        return ""

    monkeypatch.setattr(pd, "_find_entity", find)
    monkeypatch.setattr(pd, "HAWebSocket", _FakeWS)
    monkeypatch.setattr(pd, "_assign_pe_pipeline", lambda token: None)
    assert pd.ensure_assist_pipeline("tok", "conversation.sol") is True
    create = _FakeWS.created[0]
    assert create["tts_engine"] == "tts.piper"
    assert create["tts_language"] == "de_DE"
    assert create["tts_voice"] is None


def test_existing_pipeline_converges_onto_martin(pd, monkeypatch):
    # A box wired with piper before the Martin units landed gets its
    # pipeline updated to the bridge entity on the next deploy.
    _FakeWS.created, _FakeWS.preferred = [], []
    _FakeWS.updated = []
    _FakeWS.pipelines = [
        {
            "name": "Sol",
            "id": "p-old",
            "tts_engine": "tts.piper",
            "tts_language": "de_DE",
            "tts_voice": None,
            "conversation_engine": "conversation.sol",
            "conversation_language": "de",
            "language": "de",
            "stt_engine": "stt.faster_whisper",
            "stt_language": "de",
            "wake_word_entity": None,
            "wake_word_id": None,
        }
    ]

    def find(token, prefix, needle=""):
        if prefix == "stt.":
            return "stt.faster_whisper"
        if prefix == "tts." and needle == "openai":
            return "tts.openai_streaming"
        if prefix == "tts.":
            return "tts.piper"
        return ""

    class _WS(_FakeWS):
        def cmd(self, payload):
            if payload["type"] == "assist_pipeline/pipeline/update":
                _FakeWS.updated.append(payload)
                return payload
            return super().cmd(payload)

    monkeypatch.setattr(pd, "_find_entity", find)
    monkeypatch.setattr(pd, "HAWebSocket", _WS)
    monkeypatch.setattr(pd, "_assign_pe_pipeline", lambda token: None)
    assert pd.ensure_assist_pipeline("tok", "conversation.sol") is True
    assert _FakeWS.created == []
    upd = _FakeWS.updated[0]
    assert upd["tts_engine"] == "tts.openai_streaming"
    assert upd["tts_language"] == "de"
    assert upd["tts_voice"] == "kokoro"
    assert _FakeWS.preferred == ["p-old"]


def test_pipeline_idempotent_on_name(pd, monkeypatch):
    _FakeWS.created, _FakeWS.preferred = [], []
    _FakeWS.pipelines = [{"name": "Sol", "id": "p-existing"}]
    entity_map = {"stt.": "stt.x", "tts.": "tts.y"}
    monkeypatch.setattr(
        pd,
        "_find_entity",
        lambda token, prefix, needle="": entity_map.get(prefix, ""),
    )
    monkeypatch.setattr(pd, "HAWebSocket", _FakeWS)
    monkeypatch.setattr(pd, "_assign_pe_pipeline", lambda token: None)
    assert pd.ensure_assist_pipeline("tok", "conversation.sol") is True
    assert _FakeWS.created == []
    assert _FakeWS.preferred == ["p-existing"]


def test_wire_skips_without_token(pd, monkeypatch):
    monkeypatch.setattr(
        pd,
        "ensure_wyoming_entry",
        lambda *a, **k: pytest.fail("must not wire without a token"),
    )
    pd.wire_voice_pipeline("", "8787", "key")


def test_wire_skips_agent_when_engine_down(pd, monkeypatch):
    wired = []
    monkeypatch.setattr(pd, "ensure_wyoming_entry", lambda *a, **k: wired.append(a[1]))
    monkeypatch.setattr(pd, "wait_for_chat", lambda port, timeout_secs=120: False)
    monkeypatch.setattr(
        pd,
        "ensure_conversation_agent",
        lambda *a: pytest.fail("engine down — no agent"),
    )
    pd.wire_voice_pipeline("tok", "8787", "key")
    assert wired == ["whisper", "piper"]

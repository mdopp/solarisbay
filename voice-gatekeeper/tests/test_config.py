"""Tests for the env-driven Settings dataclass."""

from __future__ import annotations

import importlib

import pytest


def _fresh_settings(monkeypatch, env: dict[str, str]):
    """Reload the config module so Settings.from_env() picks up new env."""
    import gatekeeper.config as cfg_mod

    for key in list(env.keys()):
        monkeypatch.setenv(key, env[key])
    importlib.reload(cfg_mod)
    return cfg_mod.Settings.from_env()


def test_voice_pe_devices_parses_json_map(monkeypatch):
    s = _fresh_settings(
        monkeypatch,
        {
            "HERMES_URL": "http://hermes:8000",
            "VOICE_PE_DEVICES": '{"office": "tcp://10.0.0.1:10700", "bedroom": "tcp://10.0.0.2:10700"}',
        },
    )
    assert s.voice_pe_devices == {
        "office": "tcp://10.0.0.1:10700",
        "bedroom": "tcp://10.0.0.2:10700",
    }


def test_voice_pe_devices_invalid_json_is_empty(monkeypatch):
    s = _fresh_settings(
        monkeypatch,
        {"HERMES_URL": "http://hermes:8000", "VOICE_PE_DEVICES": "not-json"},
    )
    assert s.voice_pe_devices == {}


def test_voice_pe_devices_empty_when_unset(monkeypatch):
    monkeypatch.delenv("VOICE_PE_DEVICES", raising=False)
    s = _fresh_settings(monkeypatch, {"HERMES_URL": "http://hermes:8000"})
    assert s.voice_pe_devices == {}


def test_push_port_default(monkeypatch):
    monkeypatch.delenv("PUSH_PORT", raising=False)
    s = _fresh_settings(monkeypatch, {"HERMES_URL": "http://hermes:8000"})
    assert s.push_port == 10750


def test_push_and_mcp_hosts_default_to_loopback(monkeypatch):
    # #116: under hostNetwork a 0.0.0.0 bind exposes these on the LAN
    # where a blank token is unauthenticated. They only ever serve Hermes
    # over loopback, so the default must stay 127.0.0.1.
    monkeypatch.delenv("PUSH_HOST", raising=False)
    monkeypatch.delenv("MCP_HOST", raising=False)
    s = _fresh_settings(monkeypatch, {"HERMES_URL": "http://hermes:8000"})
    assert s.push_host == "127.0.0.1"
    assert s.mcp_host == "127.0.0.1"


def test_push_and_mcp_hosts_overridable(monkeypatch):
    s = _fresh_settings(
        monkeypatch,
        {
            "HERMES_URL": "http://hermes:8000",
            "PUSH_HOST": "0.0.0.0",
            "MCP_HOST": "0.0.0.0",
        },
    )
    assert s.push_host == "0.0.0.0"
    assert s.mcp_host == "0.0.0.0"


def test_fast_hermes_model_empty_when_unset(monkeypatch):
    monkeypatch.delenv("FAST_HERMES_MODEL", raising=False)
    s = _fresh_settings(monkeypatch, {"HERMES_URL": "http://hermes:8000"})
    assert s.fast_hermes_model == ""


def test_fast_hermes_model_read_and_trimmed(monkeypatch):
    s = _fresh_settings(
        monkeypatch,
        {"HERMES_URL": "http://hermes:8000", "FAST_HERMES_MODEL": "  gemma4:e2b  "},
    )
    assert s.fast_hermes_model == "gemma4:e2b"


def test_hermes_url_is_required(monkeypatch):
    monkeypatch.delenv("HERMES_URL", raising=False)
    import gatekeeper.config as cfg_mod

    with pytest.raises(KeyError):
        cfg_mod.Settings.from_env()


def test_settings_has_single_hermes_url_no_admin_gateway(monkeypatch):
    # Voice routes to the household gateway only (#293): residents speak to Sol,
    # never the admin profile. The gatekeeper carries exactly one Hermes URL and
    # has no admin-gateway field, so a voice turn can never reach hermes-admin.
    s = _fresh_settings(monkeypatch, {"HERMES_URL": "http://127.0.0.1:8642"})
    assert s.hermes_url == "http://127.0.0.1:8642"
    fields = set(type(s).__dataclass_fields__)
    assert not any("admin" in name for name in fields)
    # No stray admin-gateway env is consulted, even if one is present.
    monkeypatch.setenv("HERMES_ADMIN_URL", "http://127.0.0.1:8643")
    s2 = _fresh_settings(monkeypatch, {"HERMES_URL": "http://127.0.0.1:8642"})
    assert s2.hermes_url == "http://127.0.0.1:8642"

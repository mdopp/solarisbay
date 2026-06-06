"""Tests for the ServiceBay-MCP token self-heal in the hermes /
solbay post-deploy scripts (#126).

These post-deploy files have hyphenated names and live under templates/,
so they're loaded via importlib rather than a normal import. The tests
monkeypatch the network helpers so no live ServiceBay is needed.
"""

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
def hermes():
    return _load("hermes_post_deploy", TEMPLATES / "hermes" / "post-deploy.py")


@pytest.fixture(scope="module")
def household():
    return _load("household_post_deploy", TEMPLATES / "solbay" / "post-deploy.py")


GOOD = "sb_0a1b2c3d_ABCDEF234567"
JUNK = "9f3c-random-opaque-fallback"


# ── token-shape validation ──────────────────────────────────────────────


def test_shape_regex_accepts_minted_only(hermes, household):
    assert hermes.SB_MCP_TOKEN_RE.match(GOOD)
    assert household.SB_MCP_TOKEN_RE.match(GOOD)
    for bad in (JUNK, "sb_xyz_ABC", "sb_0a1b2c3d_", "Bearer sb_0a1b2c3d_AB"):
        assert not hermes.SB_MCP_TOKEN_RE.match(bad)


# ── hermes: mint retry + never persist junk ──────────────────────────────


def test_mint_retries_then_succeeds(hermes, monkeypatch):
    calls = {"n": 0}

    def fake_once(sb_api, name):
        calls["n"] += 1
        return GOOD if calls["n"] >= 3 else None

    monkeypatch.setattr(hermes, "_provision_sb_mcp_token_once", fake_once)
    monkeypatch.setattr(hermes.time, "sleep", lambda *_: None)
    assert hermes.provision_sb_mcp_token("http://sb") == GOOD
    assert calls["n"] == 3


def test_mint_returns_none_after_exhausting_retries(hermes, monkeypatch):
    monkeypatch.setattr(hermes, "_provision_sb_mcp_token_once", lambda *_: None)
    monkeypatch.setattr(hermes.time, "sleep", lambda *_: None)
    assert hermes.provision_sb_mcp_token("http://sb") is None


def test_mint_once_rejects_non_sb_shaped_secret(hermes, monkeypatch):
    monkeypatch.setattr(hermes, "post_json", lambda *a, **k: (200, {"secret": JUNK}))
    assert hermes._provision_sb_mcp_token_once("http://sb", "hermes-mcp") is None


# ── hermes: self-heal a junk token on redeploy ───────────────────────────

_CONFIG_WITH_JUNK = (
    "model:\n  provider: custom\n"
    "mcp_servers:\n"
    "  servicebay:\n"
    '    url: "http://127.0.0.1:5888/mcp"\n'
    "    headers:\n"
    f'      Authorization: "Bearer {JUNK}"\n'
)

_CONFIG_WITH_GOOD = _CONFIG_WITH_JUNK.replace(JUNK, GOOD)


def test_self_heal_rewrites_junk_token(hermes, tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(_CONFIG_WITH_JUNK)
    monkeypatch.setattr(hermes, "provision_sb_mcp_token", lambda *a, **k: GOOD)
    monkeypatch.setattr(hermes, "probe_sb_mcp_token", lambda t: False)

    changed = hermes.ensure_sb_mcp_servers_block(str(cfg), "http://sb")
    assert changed is True
    out = cfg.read_text()
    assert JUNK not in out
    assert f"Bearer {GOOD}" in out
    # Exactly one servicebay entry remains.
    assert out.count("servicebay:") == 1


def test_self_heal_noop_on_valid_token(hermes, tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(_CONFIG_WITH_GOOD)
    monkeypatch.setattr(hermes, "probe_sb_mcp_token", lambda t: True)

    def boom(*a, **k):  # mint must NOT be called for a valid token
        raise AssertionError("re-minted a valid token")

    monkeypatch.setattr(hermes, "provision_sb_mcp_token", boom)
    assert hermes.ensure_sb_mcp_servers_block(str(cfg), "http://sb") is False
    assert cfg.read_text() == _CONFIG_WITH_GOOD


def test_self_heal_skips_when_remint_fails(hermes, tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(_CONFIG_WITH_JUNK)
    monkeypatch.setattr(hermes, "probe_sb_mcp_token", lambda t: False)
    monkeypatch.setattr(hermes, "provision_sb_mcp_token", lambda *a, **k: None)
    # Mint failed → leave the file untouched, signal no restart.
    assert hermes.ensure_sb_mcp_servers_block(str(cfg), "http://sb") is False
    assert cfg.read_text() == _CONFIG_WITH_JUNK


def test_first_install_appends_block(hermes, tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("model:\n  provider: custom\n")
    monkeypatch.setattr(hermes, "provision_sb_mcp_token", lambda *a, **k: GOOD)
    assert hermes.ensure_sb_mcp_servers_block(str(cfg), "http://sb") is True
    out = cfg.read_text()
    assert "mcp_servers:" in out and f"Bearer {GOOD}" in out


# ── solbay: never persist junk, self-heal via rewrite ────────────


def test_household_collect_skips_when_mint_fails(household, monkeypatch):
    monkeypatch.setattr(household, "SERVICEBAY_MCP_URL", "http://127.0.0.1:5888/mcp")
    monkeypatch.setattr(household, "SERVICEBAY_MCP_TOKEN", JUNK)
    monkeypatch.setattr(household, "GATEKEEPER_MCP_URL", "")
    monkeypatch.setattr(household, "ABS_API_KEY", "")
    monkeypatch.setattr(household, "existing_servicebay_mcp_token", lambda: None)
    monkeypatch.setattr(household, "mint_servicebay_mcp_token", lambda *a, **k: None)

    servers = household.collect_mcp_servers()
    # Junk fallback must never be spliced in.
    assert all(name != "servicebay-mcp" for name, _, _ in servers)
    assert JUNK not in [tok for _, _, tok in servers]


def test_household_collect_keeps_valid_existing(household, monkeypatch):
    monkeypatch.setattr(household, "SERVICEBAY_MCP_URL", "http://127.0.0.1:5888/mcp")
    monkeypatch.setattr(household, "GATEKEEPER_MCP_URL", "")
    monkeypatch.setattr(household, "ABS_API_KEY", "")
    monkeypatch.setattr(household, "existing_servicebay_mcp_token", lambda: GOOD)
    monkeypatch.setattr(household, "probe_servicebay_mcp_token", lambda t: True)

    def boom(*a, **k):
        raise AssertionError("re-minted a valid token")

    monkeypatch.setattr(household, "mint_servicebay_mcp_token", boom)
    servers = household.collect_mcp_servers()
    assert ("servicebay-mcp", "http://127.0.0.1:5888/mcp", GOOD) in servers


def test_household_collect_mints_when_existing_invalid(household, monkeypatch):
    monkeypatch.setattr(household, "SERVICEBAY_MCP_URL", "http://127.0.0.1:5888/mcp")
    monkeypatch.setattr(household, "GATEKEEPER_MCP_URL", "")
    monkeypatch.setattr(household, "ABS_API_KEY", "")
    monkeypatch.setattr(household, "existing_servicebay_mcp_token", lambda: None)
    monkeypatch.setattr(household, "mint_servicebay_mcp_token", lambda *a, **k: GOOD)
    servers = household.collect_mcp_servers()
    assert ("servicebay-mcp", "http://127.0.0.1:5888/mcp", GOOD) in servers


def test_household_existing_token_parser_rejects_junk(household, monkeypatch):
    # solbay writes its entry under the `servicebay-mcp:` key (the live box
    # config uses it), which is what existing_servicebay_mcp_token() parses —
    # the shared fixture uses the hermes-side `servicebay:` key, so reshape it.
    hh_junk = _CONFIG_WITH_JUNK.replace("servicebay:", "servicebay-mcp:")
    hh_good = _CONFIG_WITH_GOOD.replace("servicebay:", "servicebay-mcp:")
    monkeypatch.setattr(household, "read_config_via_container", lambda: hh_junk)
    assert household.existing_servicebay_mcp_token() is None
    monkeypatch.setattr(household, "read_config_via_container", lambda: hh_good)
    assert household.existing_servicebay_mcp_token() == GOOD


def test_household_mint_uses_canonical_route_and_validates(household, monkeypatch):
    seen = {}

    def fake_post(path, payload, timeout=30.0):
        seen["path"] = path
        return 200, {"secret": GOOD}

    monkeypatch.setattr(household, "sb_post_json", fake_post)
    assert household._mint_servicebay_mcp_token_once() == GOOD
    assert seen["path"] == "/api/system/api-tokens"

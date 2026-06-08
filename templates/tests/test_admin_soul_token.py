"""Tests for the admin-soul post-deploy: full-admin token mint + the
servicebay_admin entry self-heal (#175).

Like the sibling token tests, the hyphenated post-deploy.py is loaded via
importlib and the network helpers are monkeypatched so no live ServiceBay /
hermes container is needed.
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
def admin():
    return _load("admin_soul_post_deploy", TEMPLATES / "solilos" / "post-deploy.py")


GOOD = "sb_0a1b2c3d_ABCDEF234567"
GOOD2 = "sb_1122aabb_ZZZZ234567"
JUNK = "9f3c-random-opaque-fallback"

_CONFIG_NO_MCP = "model:\n  provider: custom\n"
_CONFIG_HOUSEHOLD_ONLY = (
    "model:\n  provider: custom\n"
    "mcp_servers:\n"
    "  servicebay-mcp:\n"
    '    url: "http://127.0.0.1:5888/mcp"\n'
    "    headers:\n"
    f'      Authorization: "Bearer {GOOD}"\n'
)
_CONFIG_ADMIN_JUNK = _CONFIG_HOUSEHOLD_ONLY + (
    f"  {'servicebay_admin'}:\n"
    '    url: "http://127.0.0.1:5888/mcp"\n'
    "    headers:\n"
    f'      Authorization: "Bearer {JUNK}"\n'
)
_CONFIG_ADMIN_GOOD = _CONFIG_ADMIN_JUNK.replace(JUNK, GOOD2)


# ── scopes / token name guardrail ─────────────────────────────────────────


def test_admin_scopes_are_read_lifecycle_mutate_no_destroy(admin):
    assert admin.ADMIN_MCP_SCOPES == ["read", "lifecycle", "mutate"]
    assert "destroy" not in admin.ADMIN_MCP_SCOPES


def test_admin_entry_name_is_distinct(admin):
    assert admin.ADMIN_MCP_NAME == "servicebay_admin"


# ── mint: canonical route, full-admin scopes, never persist junk ──────────


def test_mint_uses_canonical_route_and_admin_scopes(admin, monkeypatch):
    seen = {}

    def fake_post(url, payload, timeout=30.0):
        seen["url"] = url
        seen["scopes"] = payload.get("scopes")
        return 200, {"secret": GOOD}

    monkeypatch.setattr(admin, "post_json", fake_post)
    assert admin._mint_admin_token_once() == GOOD
    assert seen["url"].endswith("/api/system/api-tokens")
    assert seen["scopes"] == ["read", "lifecycle", "mutate"]


def test_mint_rejects_non_sb_shaped_secret(admin, monkeypatch):
    monkeypatch.setattr(admin, "post_json", lambda *a, **k: (200, {"secret": JUNK}))
    assert admin._mint_admin_token_once() is None


def test_mint_retries_then_succeeds(admin, monkeypatch):
    calls = {"n": 0}

    def fake_once():
        calls["n"] += 1
        return GOOD if calls["n"] >= 3 else None

    monkeypatch.setattr(admin, "_mint_admin_token_once", fake_once)
    monkeypatch.setattr(admin.time, "sleep", lambda *_: None)
    assert admin.mint_admin_token() == GOOD
    assert calls["n"] == 3


def test_mint_returns_none_after_exhausting_retries(admin, monkeypatch):
    monkeypatch.setattr(admin, "_mint_admin_token_once", lambda: None)
    monkeypatch.setattr(admin.time, "sleep", lambda *_: None)
    assert admin.mint_admin_token() is None


# ── ensure_admin_mcp_entry: first install / self-heal / no-op ─────────────


def test_first_install_appends_admin_entry(admin, monkeypatch):
    written = {}
    monkeypatch.setattr(admin, "read_config_via_container", lambda: _CONFIG_NO_MCP)
    monkeypatch.setattr(
        admin, "write_config_via_container", lambda c: written.update(c=c) or True
    )
    monkeypatch.setattr(admin, "mint_admin_token", lambda *a, **k: GOOD)

    assert admin.ensure_admin_mcp_entry() is True
    out = written["c"]
    assert "mcp_servers:" in out
    assert "servicebay_admin:" in out
    assert f"Bearer {GOOD}" in out


def test_appends_alongside_household_entry_without_touching_it(admin, monkeypatch):
    written = {}
    monkeypatch.setattr(
        admin, "read_config_via_container", lambda: _CONFIG_HOUSEHOLD_ONLY
    )
    monkeypatch.setattr(
        admin, "write_config_via_container", lambda c: written.update(c=c) or True
    )
    monkeypatch.setattr(admin, "mint_admin_token", lambda *a, **k: GOOD2)

    assert admin.ensure_admin_mcp_entry() is True
    out = written["c"]
    # Household entry preserved verbatim, admin entry added.
    assert "servicebay-mcp:" in out
    assert f"Bearer {GOOD}" in out  # household token untouched
    assert "servicebay_admin:" in out
    assert f"Bearer {GOOD2}" in out
    assert out.count("servicebay_admin:") == 1


def test_self_heal_rewrites_junk_admin_token_only(admin, monkeypatch):
    written = {}
    monkeypatch.setattr(admin, "read_config_via_container", lambda: _CONFIG_ADMIN_JUNK)
    monkeypatch.setattr(
        admin, "write_config_via_container", lambda c: written.update(c=c) or True
    )
    monkeypatch.setattr(admin, "probe_admin_token", lambda t: False)
    monkeypatch.setattr(admin, "mint_admin_token", lambda *a, **k: GOOD2)

    assert admin.ensure_admin_mcp_entry() is True
    out = written["c"]
    assert JUNK not in out
    assert f"Bearer {GOOD2}" in out
    assert out.count("servicebay_admin:") == 1
    # Household entry left intact during the heal.
    assert "servicebay-mcp:" in out
    assert f"Bearer {GOOD}" in out


def test_noop_on_valid_admin_token(admin, monkeypatch):
    monkeypatch.setattr(admin, "read_config_via_container", lambda: _CONFIG_ADMIN_GOOD)
    monkeypatch.setattr(admin, "probe_admin_token", lambda t: True)

    def boom(*a, **k):
        raise AssertionError("re-minted a valid token")

    monkeypatch.setattr(admin, "mint_admin_token", boom)

    def no_write(*a, **k):
        raise AssertionError("rewrote config for a valid token")

    monkeypatch.setattr(admin, "write_config_via_container", no_write)
    assert admin.ensure_admin_mcp_entry() is False


def test_skips_when_mint_fails_never_persists_junk(admin, monkeypatch):
    monkeypatch.setattr(admin, "read_config_via_container", lambda: _CONFIG_ADMIN_JUNK)
    monkeypatch.setattr(admin, "probe_admin_token", lambda t: False)
    monkeypatch.setattr(admin, "mint_admin_token", lambda *a, **k: None)

    def no_write(*a, **k):
        raise AssertionError("wrote config despite mint failure")

    monkeypatch.setattr(admin, "write_config_via_container", no_write)
    assert admin.ensure_admin_mcp_entry() is False


def test_noop_when_config_absent(admin, monkeypatch):
    monkeypatch.setattr(admin, "read_config_via_container", lambda: None)
    assert admin.ensure_admin_mcp_entry() is False

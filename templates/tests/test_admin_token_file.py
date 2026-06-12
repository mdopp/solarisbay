"""Tests for the servicebay_admin token file (Phase 3): minted via the SB
API (scopes read+lifecycle+mutate, never destroy/exec) and dropped at
<DATA_DIR>/solbay/sb-admin-token for the engine's admin MCP toolbox."""

from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

TEMPLATES = pathlib.Path(__file__).resolve().parents[1]
GOOD = "sb_0123abcd_ABCDEFG234567"
JUNK = "not-a-token"


def _load(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def pd():
    return _load("solilos_pd_admin", TEMPLATES / "solilos" / "post-deploy.py")


@pytest.fixture
def data_dir(tmp_path):
    (tmp_path / "solbay").mkdir()
    return tmp_path


def _token_path(data_dir):
    return data_dir / "solbay" / "sb-admin-token"


def test_admin_scopes_no_destroy_no_exec(pd):
    assert pd.ADMIN_MCP_SCOPES == ["read", "lifecycle", "mutate"]


def test_mint_uses_canonical_route_and_scopes(pd, monkeypatch):
    calls = []

    def fake_post(url, payload, timeout=10.0):
        calls.append((url, payload))
        return 200, {"secret": GOOD}

    monkeypatch.setattr(pd, "post_json", fake_post)
    assert pd.mint_admin_token("http://sb:3000") == GOOD
    url, payload = calls[0]
    assert url.endswith("/api/system/api-tokens")
    assert payload["scopes"] == ["read", "lifecycle", "mutate"]


def test_mint_rejects_non_sb_shaped_secret(pd, monkeypatch):
    monkeypatch.setattr(pd, "post_json", lambda *a, **k: (200, {"secret": JUNK}))
    assert pd.mint_admin_token("http://sb:3000", attempts=1) is None


def test_file_written_0600_on_mint(pd, data_dir, monkeypatch):
    monkeypatch.setattr(pd, "mint_admin_token", lambda sb: GOOD)
    assert pd.ensure_admin_token_file(str(data_dir), "http://sb", "http://mcp") is True
    path = _token_path(data_dir)
    assert path.read_text().strip() == GOOD
    assert (path.stat().st_mode & 0o777) == 0o600


def test_valid_existing_token_kept(pd, data_dir, monkeypatch):
    _token_path(data_dir).write_text(GOOD + "\n")
    monkeypatch.setattr(pd, "probe_admin_token", lambda t, u: True)
    monkeypatch.setattr(
        pd, "mint_admin_token", lambda sb: pytest.fail("must not re-mint")
    )
    assert pd.ensure_admin_token_file(str(data_dir), "http://sb", "http://mcp") is True


def test_junk_existing_token_replaced(pd, data_dir, monkeypatch):
    _token_path(data_dir).write_text(JUNK + "\n")
    monkeypatch.setattr(pd, "mint_admin_token", lambda sb: GOOD)
    assert pd.ensure_admin_token_file(str(data_dir), "http://sb", "http://mcp") is True
    assert _token_path(data_dir).read_text().strip() == GOOD


def test_stale_token_replaced_when_probe_401s(pd, data_dir, monkeypatch):
    _token_path(data_dir).write_text(GOOD + "\n")
    monkeypatch.setattr(pd, "probe_admin_token", lambda t, u: False)
    monkeypatch.setattr(pd, "mint_admin_token", lambda sb: "sb_99999999_NEWTOKEN234")
    assert pd.ensure_admin_token_file(str(data_dir), "http://sb", "http://mcp") is True
    assert _token_path(data_dir).read_text().strip() == "sb_99999999_NEWTOKEN234"


def test_mint_failure_writes_nothing(pd, data_dir, monkeypatch):
    monkeypatch.setattr(pd, "mint_admin_token", lambda sb: None)
    assert pd.ensure_admin_token_file(str(data_dir), "http://sb", "http://mcp") is False
    assert not _token_path(data_dir).exists()

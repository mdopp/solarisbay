"""Tests for the non-expiring read-only SB token file (#818, servicebay#2302):
minted once via the SB API with {scopes:["read"], neverExpires:true} and dropped
at <DATA_DIR>/solarisbay/sb-read-token so the unattended pollers don't 401-churn
when the rotating admin token lapses. Idempotent: an existing file is kept; a
missing file with the named token still present in SB (secret unrecoverable) is
left on the fallback rather than minting a duplicate."""

from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

TEMPLATES = pathlib.Path(__file__).resolve().parents[1]
GOOD = "sb_0123abcd_ABCDEFG234567"
ENV_GOOD = "sb_beef1234_ZZZZ234567AB"
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
    return _load("solaris_pd_read", TEMPLATES / "solaris" / "post-deploy.py")


@pytest.fixture
def data_dir(tmp_path):
    (tmp_path / "solarisbay").mkdir()
    return tmp_path


def _token_path(data_dir):
    return data_dir / "solarisbay" / "sb-read-token"


def test_read_scopes_are_read_only(pd):
    assert pd.READ_TOKEN_SCOPES == ["read"]


def test_mint_sends_neverexpires_read_only(pd, monkeypatch):
    calls = []

    def fake_post(url, payload, timeout=10.0):
        calls.append((url, payload))
        return 200, {"secret": GOOD}

    monkeypatch.setattr(pd, "post_json", fake_post)
    assert pd.mint_read_token("http://sb:3000") == GOOD
    url, payload = calls[0]
    assert url.endswith("/api/system/api-tokens")
    assert payload["scopes"] == ["read"]
    assert payload["neverExpires"] is True


def test_mint_rejects_non_sb_shaped_secret(pd, monkeypatch):
    monkeypatch.setattr(pd, "post_json", lambda *a, **k: (200, {"secret": JUNK}))
    assert pd.mint_read_token("http://sb:3000", attempts=1) is None


def test_file_written_0600_on_mint(pd, data_dir, monkeypatch):
    monkeypatch.setattr(pd, "read_token_exists", lambda sb: False)
    monkeypatch.setattr(pd, "mint_read_token", lambda sb: GOOD)
    assert pd.ensure_read_token_file(str(data_dir), "http://sb") is True
    path = _token_path(data_dir)
    assert path.read_text().strip() == GOOD
    assert (path.stat().st_mode & 0o777) == 0o600


def test_existing_file_kept_without_minting(pd, data_dir, monkeypatch):
    _token_path(data_dir).write_text(GOOD + "\n")
    monkeypatch.setattr(
        pd, "mint_read_token", lambda sb: pytest.fail("must not re-mint")
    )
    monkeypatch.setattr(
        pd, "read_token_exists", lambda sb: pytest.fail("must not query when file OK")
    )
    assert pd.ensure_read_token_file(str(data_dir), "http://sb") is True


def test_missing_file_but_token_exists_leaves_fallback(pd, data_dir, monkeypatch):
    # SB already has the named token but the file is gone → the secret is
    # unrecoverable; must NOT mint a duplicate, leave the pollers on the fallback.
    monkeypatch.setattr(pd, "read_token_exists", lambda sb: True)
    monkeypatch.setattr(
        pd, "mint_read_token", lambda sb: pytest.fail("must not mint a duplicate")
    )
    assert pd.ensure_read_token_file(str(data_dir), "http://sb") is False
    assert not _token_path(data_dir).exists()


def test_junk_existing_file_replaced(pd, data_dir, monkeypatch):
    _token_path(data_dir).write_text(JUNK + "\n")
    monkeypatch.setattr(pd, "read_token_exists", lambda sb: False)
    monkeypatch.setattr(pd, "mint_read_token", lambda sb: GOOD)
    assert pd.ensure_read_token_file(str(data_dir), "http://sb") is True
    assert _token_path(data_dir).read_text().strip() == GOOD


def test_mint_failure_writes_nothing(pd, data_dir, monkeypatch):
    monkeypatch.setattr(pd, "read_token_exists", lambda sb: False)
    monkeypatch.setattr(pd, "mint_read_token", lambda sb: None)
    assert pd.ensure_read_token_file(str(data_dir), "http://sb") is False
    assert not _token_path(data_dir).exists()


def test_env_token_written_without_minting(pd, data_dir, monkeypatch):
    # servicebay#2317: SB injects a well-formed SB_READ_TOKEN → write it to the
    # file, never touch the legacy mint path.
    monkeypatch.setenv("SB_READ_TOKEN", ENV_GOOD)
    monkeypatch.setattr(
        pd, "mint_read_token", lambda sb: pytest.fail("must not mint when env present")
    )
    monkeypatch.setattr(
        pd,
        "read_token_exists",
        lambda sb: pytest.fail("must not query when env present"),
    )
    assert pd.ensure_read_token_file(str(data_dir), "http://sb") is True
    path = _token_path(data_dir)
    assert path.read_text().strip() == ENV_GOOD
    assert (path.stat().st_mode & 0o777) == 0o600


def test_malformed_env_falls_through_to_mint(pd, data_dir, monkeypatch):
    monkeypatch.setenv("SB_READ_TOKEN", JUNK)
    monkeypatch.setattr(pd, "read_token_exists", lambda sb: False)
    monkeypatch.setattr(pd, "mint_read_token", lambda sb: GOOD)
    assert pd.ensure_read_token_file(str(data_dir), "http://sb") is True
    assert _token_path(data_dir).read_text().strip() == GOOD


def test_empty_env_falls_through_to_mint(pd, data_dir, monkeypatch):
    monkeypatch.setenv("SB_READ_TOKEN", "")
    monkeypatch.setattr(pd, "read_token_exists", lambda sb: False)
    monkeypatch.setattr(pd, "mint_read_token", lambda sb: GOOD)
    assert pd.ensure_read_token_file(str(data_dir), "http://sb") is True
    assert _token_path(data_dir).read_text().strip() == GOOD


def test_env_same_as_file_is_idempotent_noop(pd, data_dir, monkeypatch):
    _token_path(data_dir).write_text(ENV_GOOD + "\n")
    monkeypatch.setenv("SB_READ_TOKEN", ENV_GOOD)
    monkeypatch.setattr(
        pd, "mint_read_token", lambda sb: pytest.fail("must not mint on no-op")
    )
    assert pd.ensure_read_token_file(str(data_dir), "http://sb") is True
    assert _token_path(data_dir).read_text().strip() == ENV_GOOD


def test_env_differs_from_file_updates_to_env(pd, data_dir, monkeypatch):
    # SB revokes+re-mints each deploy → the env token is authoritative over a
    # stale file token, even a well-formed one.
    _token_path(data_dir).write_text(GOOD + "\n")
    monkeypatch.setenv("SB_READ_TOKEN", ENV_GOOD)
    monkeypatch.setattr(
        pd, "mint_read_token", lambda sb: pytest.fail("must not mint when env present")
    )
    assert pd.ensure_read_token_file(str(data_dir), "http://sb") is True
    assert _token_path(data_dir).read_text().strip() == ENV_GOOD


def test_read_token_exists_matches_by_name(pd, monkeypatch):
    monkeypatch.setattr(
        pd,
        "get_json",
        lambda url, timeout=10.0: (
            200,
            {"tokens": [{"name": pd.READ_TOKEN_NAME, "id": "aa"}]},
        ),
    )
    assert pd.read_token_exists("http://sb") is True
    monkeypatch.setattr(
        pd,
        "get_json",
        lambda url, timeout=10.0: (200, {"tokens": [{"name": "other", "id": "bb"}]}),
    )
    assert pd.read_token_exists("http://sb") is False

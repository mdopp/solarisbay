"""Tests for the non-expiring read-only SB token file (#818, servicebay#2317):
ServiceBay mints the durable read-scoped token itself and injects it as the
SB_READ_TOKEN env var; the post-deploy just persists it at
<DATA_DIR>/solarisbay/sb-read-token so the unattended pollers don't 401-churn
when the rotating admin token lapses. It never self-mints (durable creds are the
platform's job). Idempotent only when the file already equals the injected
token — ServiceBay revokes + re-mints it each deploy, so a merely well-formed
but stale/revoked file MUST be overwritten (else the pollers 401)."""

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


def test_env_token_written_0600(pd, data_dir, monkeypatch):
    # servicebay#2317: SB injects a well-formed SB_READ_TOKEN → write it 0600.
    monkeypatch.setenv("SB_READ_TOKEN", ENV_GOOD)
    assert pd.ensure_read_token_file(str(data_dir)) is True
    path = _token_path(data_dir)
    assert path.read_text().strip() == ENV_GOOD
    assert (path.stat().st_mode & 0o777) == 0o600


def test_existing_file_kept(pd, data_dir, monkeypatch):
    _token_path(data_dir).write_text(GOOD + "\n")
    monkeypatch.delenv("SB_READ_TOKEN", raising=False)
    assert pd.ensure_read_token_file(str(data_dir)) is True
    assert _token_path(data_dir).read_text().strip() == GOOD


def test_no_env_no_file_returns_false_without_write(pd, data_dir, monkeypatch):
    monkeypatch.delenv("SB_READ_TOKEN", raising=False)
    assert pd.ensure_read_token_file(str(data_dir)) is False
    assert not _token_path(data_dir).exists()


def test_malformed_env_no_file_returns_false(pd, data_dir, monkeypatch):
    monkeypatch.setenv("SB_READ_TOKEN", JUNK)
    assert pd.ensure_read_token_file(str(data_dir)) is False
    assert not _token_path(data_dir).exists()


def test_junk_existing_file_replaced_by_env(pd, data_dir, monkeypatch):
    _token_path(data_dir).write_text(JUNK + "\n")
    monkeypatch.setenv("SB_READ_TOKEN", ENV_GOOD)
    assert pd.ensure_read_token_file(str(data_dir)) is True
    assert _token_path(data_dir).read_text().strip() == ENV_GOOD


def test_stale_existing_token_overwritten_by_injected(pd, data_dir, monkeypatch):
    # The #818 regression: a well-formed but STALE existing token (ServiceBay
    # revoked it on this deploy and re-minted ENV_GOOD) must be overwritten, not
    # kept — else the pollers hold a revoked credential and 401 every /napi call.
    _token_path(data_dir).write_text(GOOD + "\n")  # well-formed, but != injected
    monkeypatch.setenv("SB_READ_TOKEN", ENV_GOOD)
    assert pd.ensure_read_token_file(str(data_dir)) is True
    assert _token_path(data_dir).read_text().strip() == ENV_GOOD


def test_matching_existing_token_kept_idempotent(pd, data_dir, monkeypatch):
    # When the file already equals the injected token, keep it (no needless
    # rewrite) — a redeploy that re-injects the same token is a no-op.
    _token_path(data_dir).write_text(ENV_GOOD + "\n")
    monkeypatch.setenv("SB_READ_TOKEN", ENV_GOOD)
    assert pd.ensure_read_token_file(str(data_dir)) is True
    assert _token_path(data_dir).read_text().strip() == ENV_GOOD


def test_never_touches_sb_api(pd, data_dir, monkeypatch):
    # The boundary lesson: Solaris must not mint durable creds. The function only
    # reads the injected env + writes the file — it must never hit the SB API.
    monkeypatch.setenv("SB_READ_TOKEN", ENV_GOOD)
    monkeypatch.setattr(
        pd, "post_json", lambda *a, **k: pytest.fail("must not call the SB API")
    )
    monkeypatch.setattr(
        pd, "get_json", lambda *a, **k: pytest.fail("must not call the SB API")
    )
    assert pd.ensure_read_token_file(str(data_dir)) is True
    assert _token_path(data_dir).read_text().strip() == ENV_GOOD

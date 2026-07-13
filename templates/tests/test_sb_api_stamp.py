"""Tests for stamping the SB control-plane API + internal token into the
deployed solaris.yml (#794).

The admin SB-MCP token file is written only at deploy time, but ServiceBay's
token pool rotates, so a long-lived engine eventually 401s → admin Wartung
SB-MCP tools return nothing. The engine re-mints the token at runtime on a 401,
but it needs SB_API_URL + SB_API_TOKEN. The internal token lives only in the
post-deploy's SB-injected env (not git / a template variable), so the
post-deploy stamps it into the deployed pod manifest (same mechanism as
HASS_TOKEN). Best-effort + never logs the value.
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
def pd():
    return _load("solaris_pd_sb_api", TEMPLATES / "solaris" / "post-deploy.py")


def _pod_yml(url_value: str, token_value: str) -> str:
    return (
        "apiVersion: v1\n"
        "kind: Pod\n"
        "spec:\n"
        "  containers:\n"
        "  - name: chat\n"
        "    env:\n"
        "    - name: SB_MCP_URL\n"
        '      value: "http://127.0.0.1:5888/mcp"\n'
        "    - name: SB_API_URL\n"
        f"      value: {url_value}\n"
        "    - name: SB_API_TOKEN\n"
        f"      value: {token_value}\n"
    )


def test_patch_stamps_both_env_values(pd):
    src = _pod_yml('""', '""')
    new, n = pd._patched_sb_api_env_yaml(src, "http://sb:3000", "internal-secret")
    assert n == 2
    assert '- name: SB_API_URL\n      value: "http://sb:3000"' in new
    assert '- name: SB_API_TOKEN\n      value: "internal-secret"' in new


def test_stamp_writes_manifest(pd, tmp_path, monkeypatch):
    pod_yml = tmp_path / "solaris.yml"
    pod_yml.write_text(_pod_yml('""', '""'))
    monkeypatch.setattr(pd.os.path, "expanduser", lambda _p: str(pod_yml))

    assert pd.stamp_sb_api_credentials("http://sb:3000", "internal-secret") is True
    written = pod_yml.read_text()
    assert '- name: SB_API_URL\n      value: "http://sb:3000"' in written
    assert '- name: SB_API_TOKEN\n      value: "internal-secret"' in written


def test_stamp_noop_without_token(pd, tmp_path, monkeypatch):
    pod_yml = tmp_path / "solaris.yml"
    pod_yml.write_text(_pod_yml('""', '""'))
    monkeypatch.setattr(pd.os.path, "expanduser", lambda _p: str(pod_yml))

    # No internal token ⇒ nothing to stamp; the manifest is left untouched.
    assert pd.stamp_sb_api_credentials("http://sb:3000", "") is False
    assert pod_yml.read_text() == _pod_yml('""', '""')


def test_stamp_missing_manifest_is_soft(pd, tmp_path, monkeypatch):
    monkeypatch.setattr(pd.os.path, "expanduser", lambda _p: str(tmp_path / "nope.yml"))
    assert pd.stamp_sb_api_credentials("http://sb:3000", "internal-secret") is False


def test_token_value_never_logged(pd, tmp_path, monkeypatch, capsys):
    pod_yml = tmp_path / "solaris.yml"
    pod_yml.write_text(_pod_yml('""', '""'))
    monkeypatch.setattr(pd.os.path, "expanduser", lambda _p: str(pod_yml))

    pd.stamp_sb_api_credentials("http://sb:3000", "top-secret-token")
    assert "top-secret-token" not in capsys.readouterr().out

"""Tests for stamp_jellyfin_cast_url — deriving JELLYFIN_CAST_URL from the box
LAN IP when it's left empty (#607).

The cast base must be LAN-reachable so a Chromecast can fetch the track, not the
engine's loopback JELLYFIN_URL. ServiceBay hands the box LAN IP to the
post-deploy as the LAN_IP env var; an empty JELLYFIN_CAST_URL is stamped to
http://<lanIp>:8096 in the deployed solaris.yml (durable on reinstall, no
hardcoded IP, no operator knob). An already-set value is left as is; an absent
LAN_IP leaves it empty so the engine config falls back to JELLYFIN_URL.
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
    return _load("solaris_pd_cast", TEMPLATES / "solaris" / "post-deploy.py")


def _pod_yml(value: str) -> str:
    return (
        "apiVersion: v1\n"
        "kind: Pod\n"
        "spec:\n"
        "  containers:\n"
        "  - name: chat\n"
        "    env:\n"
        "    - name: JELLYFIN_URL\n"
        '      value: "http://127.0.0.1:8096"\n'
        "    - name: JELLYFIN_CAST_URL\n"
        f"      value: {value}\n"
        "    - name: JELLYFIN_USERNAME\n"
        '      value: "solaris"\n'
    )


@pytest.fixture
def wired(pd, tmp_path, monkeypatch):
    """A pod yml with an empty JELLYFIN_CAST_URL, expanduser pinned at it."""
    pod_yml = tmp_path / "solaris.yml"
    pod_yml.write_text(_pod_yml('""'), encoding="utf-8")
    monkeypatch.setattr(pd.os.path, "expanduser", lambda _p: str(pod_yml))
    return tmp_path, pod_yml


def _cast_value_line(pod_yml: pathlib.Path) -> str:
    src = pod_yml.read_text(encoding="utf-8")
    block = src.split("- name: JELLYFIN_CAST_URL\n", 1)[1]
    return block.splitlines()[0].strip()


def test_derives_cast_url_from_lan_ip_when_empty(pd, wired, monkeypatch):
    data_dir, pod_yml = wired
    monkeypatch.setenv("LAN_IP", "192.168.178.100")
    monkeypatch.delenv("JELLYFIN_CAST_URL", raising=False)
    assert pd.stamp_jellyfin_cast_url(str(data_dir)) == "http://192.168.178.100:8096"
    assert _cast_value_line(pod_yml) == 'value: "http://192.168.178.100:8096"'


def test_leaves_explicit_cast_url_as_is(pd, wired, monkeypatch):
    data_dir, pod_yml = wired
    monkeypatch.setenv("LAN_IP", "192.168.178.100")
    monkeypatch.setenv("JELLYFIN_CAST_URL", "http://other.lan:8096")
    before = pod_yml.read_text(encoding="utf-8")
    assert pd.stamp_jellyfin_cast_url(str(data_dir)) is None
    # The pod manifest's empty placeholder is left untouched (the engine reads
    # the operator-set value from its own env).
    assert pod_yml.read_text(encoding="utf-8") == before


def test_leaves_empty_when_lan_ip_absent(pd, wired, monkeypatch):
    data_dir, pod_yml = wired
    monkeypatch.delenv("LAN_IP", raising=False)
    monkeypatch.delenv("JELLYFIN_CAST_URL", raising=False)
    assert pd.stamp_jellyfin_cast_url(str(data_dir)) is None
    # Empty -> the engine config falls back to JELLYFIN_URL.
    assert _cast_value_line(pod_yml) == 'value: ""'


def test_returns_none_when_pod_yml_missing(pd, wired, monkeypatch):
    data_dir, pod_yml = wired
    pod_yml.unlink()
    monkeypatch.setenv("LAN_IP", "192.168.178.100")
    monkeypatch.delenv("JELLYFIN_CAST_URL", raising=False)
    assert pd.stamp_jellyfin_cast_url(str(data_dir)) is None


def test_warns_when_no_cast_env_entry(pd, wired, monkeypatch):
    data_dir, pod_yml = wired
    monkeypatch.setenv("LAN_IP", "192.168.178.100")
    monkeypatch.delenv("JELLYFIN_CAST_URL", raising=False)
    pod_yml.write_text(
        "apiVersion: v1\nkind: Pod\nspec:\n  containers:\n  - name: chat\n",
        encoding="utf-8",
    )
    assert pd.stamp_jellyfin_cast_url(str(data_dir)) is None


def test_no_hardcoded_ip_in_committed_post_deploy():
    src = (TEMPLATES / "solaris" / "post-deploy.py").read_text(encoding="utf-8")
    assert "192.168.178.100" not in src

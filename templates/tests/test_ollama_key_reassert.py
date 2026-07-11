"""Tests for the HA ollama-integration api_key re-assert self-heal (#557).

The HA ollama integration (provides `conversation.sol`) stores an `api_key`;
when it drifts from the pod's current SOLARIS_API_KEY it goes `setup_error:
unauthorized (401)` → `conversation.sol` unavailable → the Assist pipeline
red-blinks. The integration has no async_step_reconfigure, so the post-deploy
re-asserts the key directly in HA's `.storage/core.config_entries` (matching the
facade ollama entry on domain + data.url), then restarts HA so the entry
reloads. Idempotent (writes only on real drift) + fail-soft (a missing entry /
file is a clean no-op). The api_key is never logged.
"""

from __future__ import annotations

import importlib.util
import json
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
    return _load("solaris_pd_ollama_key", TEMPLATES / "solaris" / "post-deploy.py")


FACADE = "http://127.0.0.1:8787/ollama"


def _entry(domain: str, url: str, api_key: str) -> dict:
    return {
        "entry_id": f"{domain}-1",
        "domain": domain,
        "title": domain,
        "data": {"url": url, "api_key": api_key},
    }


# ── pure decision: does the facade ollama entry need patching? ────────────────


def test_reassert_patches_only_the_facade_ollama_entry_on_drift(pd):
    entries = [
        _entry("ollama", FACADE, "stale-key"),
        _entry("ollama", "http://other:1/ollama", "unrelated-key"),
        _entry("jellyfin", FACADE, "irrelevant"),
    ]
    out, changed = pd._reassert_ollama_key_in_storage(entries, FACADE, "current-key")
    assert changed is True
    assert out[0]["data"]["api_key"] == "current-key"
    # unrelated ollama entry (different url) is never clobbered
    assert out[1]["data"]["api_key"] == "unrelated-key"
    assert out[2]["data"]["api_key"] == "irrelevant"


def test_reassert_is_noop_when_key_already_current(pd):
    entries = [_entry("ollama", FACADE, "current-key")]
    _out, changed = pd._reassert_ollama_key_in_storage(entries, FACADE, "current-key")
    assert changed is False


def test_reassert_noop_when_no_facade_ollama_entry(pd):
    entries = [_entry("ollama", "http://other:1/ollama", "k")]
    _out, changed = pd._reassert_ollama_key_in_storage(entries, FACADE, "current-key")
    assert changed is False


def test_entry_match_needs_domain_and_facade_url(pd):
    assert pd._ollama_entry_matches_facade(_entry("ollama", FACADE, "k"), FACADE)
    assert not pd._ollama_entry_matches_facade(
        _entry("ollama", "http://x/ollama", "k"), FACADE
    )
    assert not pd._ollama_entry_matches_facade(_entry("jellyfin", FACADE, "k"), FACADE)


# ── orchestrator: .storage edit + HA restart ─────────────────────────────────


def _storage(tmp_path: pathlib.Path, entries: list[dict]) -> pathlib.Path:
    path = (
        tmp_path
        / "home-assistant"
        / "homeassistant"
        / ".storage"
        / "core.config_entries"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"version": 1, "data": {"entries": entries}}), encoding="utf-8"
    )
    return path


def _stub_ha_post(pd, monkeypatch):
    calls: list[str] = []

    def _post(path, token, payload, timeout=30.0):
        calls.append(path)
        return 200, None

    monkeypatch.setattr(pd, "_ha_post", _post)
    return calls


def test_drift_patches_storage_and_restarts_ha(pd, tmp_path, monkeypatch):
    path = _storage(tmp_path, [_entry("ollama", FACADE, "stale-key")])
    calls = _stub_ha_post(pd, monkeypatch)
    pd.reassert_ollama_api_key("tok", "8787", "current-key", str(tmp_path))
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["data"]["entries"][0]["data"]["api_key"] == "current-key"
    assert calls == ["/api/services/homeassistant/restart"]


def test_current_key_no_write_no_restart(pd, tmp_path, monkeypatch):
    path = _storage(tmp_path, [_entry("ollama", FACADE, "current-key")])
    before = path.read_text(encoding="utf-8")
    calls = _stub_ha_post(pd, monkeypatch)
    pd.reassert_ollama_api_key("tok", "8787", "current-key", str(tmp_path))
    assert path.read_text(encoding="utf-8") == before
    assert calls == []  # a converged box never restarts HA


def test_missing_storage_is_clean_noop(pd, tmp_path, monkeypatch):
    calls = _stub_ha_post(pd, monkeypatch)
    pd.reassert_ollama_api_key("tok", "8787", "current-key", str(tmp_path))
    assert calls == []  # no crash, no restart


def test_empty_api_key_is_skipped(pd, tmp_path, monkeypatch):
    path = _storage(tmp_path, [_entry("ollama", FACADE, "stale-key")])
    before = path.read_text(encoding="utf-8")
    calls = _stub_ha_post(pd, monkeypatch)
    pd.reassert_ollama_api_key("tok", "8787", "", str(tmp_path))
    assert path.read_text(encoding="utf-8") == before
    assert calls == []


def test_api_key_never_logged(pd, tmp_path, monkeypatch, capsys):
    _storage(tmp_path, [_entry("ollama", FACADE, "stale-key")])
    _stub_ha_post(pd, monkeypatch)
    pd.reassert_ollama_api_key("tok", "8787", "super-secret-key", str(tmp_path))
    out = capsys.readouterr().out
    assert "super-secret-key" not in out

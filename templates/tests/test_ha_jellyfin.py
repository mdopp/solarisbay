"""Tests for the HA Jellyfin integration auto-install in the solaris
post-deploy (#195).

ensure_ha_jellyfin_integration drives Home Assistant's config-entries flow
API to create the `jellyfin` config entry (so `media_player.jellyfin_*`
entities appear for Solaris to control via the existing homeassistant toolset).
It is idempotent (skips if the entry already exists) and fail-soft (never
crashes the deploy). The post-deploy has a hyphenated filename under
templates/, so it's loaded via importlib (same pattern as the sibling tests).
The HA network helpers are monkeypatched so no live HA is needed.
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
def post_deploy():
    return _load("solaris_post_deploy", TEMPLATES / "solaris" / "post-deploy.py")


def _wire(post_deploy, monkeypatch, *, entries, flow_start, flow_submit):
    """Patch the HA helpers with canned responses and record the calls.

    entries: (status, body) for GET /config_entries/entry
    flow_start: (status, body) for POST /config_entries/flow
    flow_submit: (status, body) for POST /config_entries/flow/<id>
    """
    calls = {"posts": [], "deletes": []}

    def fake_get(path, token, timeout=10.0):
        assert path == "/api/config/config_entries/entry"
        return entries

    def fake_post(path, token, payload, timeout=30.0):
        calls["posts"].append((path, payload))
        if path == "/api/config/config_entries/flow":
            return flow_start
        return flow_submit

    def fake_delete(path, token, timeout=10.0):
        calls["deletes"].append(path)

    monkeypatch.setattr(post_deploy, "_ha_get", fake_get)
    monkeypatch.setattr(post_deploy, "_ha_post", fake_post)
    monkeypatch.setattr(post_deploy, "_ha_request_delete", fake_delete)
    return calls


def test_creates_entry_when_absent(post_deploy, monkeypatch):
    calls = _wire(
        post_deploy,
        monkeypatch,
        entries=(200, [{"domain": "cast"}, {"domain": "androidtv"}]),
        flow_start=(200, {"flow_id": "FLOW1", "step_id": "user"}),
        flow_submit=(200, {"type": "create_entry", "title": "media"}),
    )
    assert (
        post_deploy.ensure_ha_jellyfin_integration(
            "tok", "http://127.0.0.1:8096", "sol", "pw"
        )
        is True
    )
    # Started the flow, then submitted the single-step user schema with all
    # three fields, and did NOT abort a successful flow.
    assert calls["posts"][0] == (
        "/api/config/config_entries/flow",
        {"handler": "jellyfin"},
    )
    assert calls["posts"][1] == (
        "/api/config/config_entries/flow/FLOW1",
        {"url": "http://127.0.0.1:8096", "username": "sol", "password": "pw"},
    )
    assert calls["deletes"] == []


def test_skips_when_entry_exists(post_deploy, monkeypatch):
    calls = _wire(
        post_deploy,
        monkeypatch,
        entries=(200, [{"domain": "jellyfin"}, {"domain": "cast"}]),
        flow_start=(200, {"flow_id": "X"}),
        flow_submit=(200, {"type": "create_entry"}),
    )
    assert (
        post_deploy.ensure_ha_jellyfin_integration("tok", "http://h:8096", "sol", "")
        is False
    )
    # Idempotent: never even starts a flow.
    assert calls["posts"] == []


@pytest.mark.parametrize(
    "url,user",
    [("", "sol"), ("http://h:8096", ""), ("", "")],
)
def test_skips_when_creds_unset(post_deploy, monkeypatch, url, user):
    # No HA call at all when URL or username is blank.
    monkeypatch.setattr(
        post_deploy,
        "_ha_get",
        lambda *a, **k: pytest.fail("should not call HA when creds unset"),
    )
    assert post_deploy.ensure_ha_jellyfin_integration("tok", url, user, "pw") is False


def test_fail_soft_when_jellyfin_unreachable(post_deploy, monkeypatch):
    # Flow submit returns a form again with errors (HA couldn't reach
    # Jellyfin / bad creds) — abort the dangling flow, return False.
    calls = _wire(
        post_deploy,
        monkeypatch,
        entries=(200, []),
        flow_start=(200, {"flow_id": "FLOW2"}),
        flow_submit=(200, {"type": "form", "errors": {"base": "cannot_connect"}}),
    )
    assert (
        post_deploy.ensure_ha_jellyfin_integration("tok", "http://h:8096", "sol", "pw")
        is False
    )
    assert calls["deletes"] == ["/api/config/config_entries/flow/FLOW2"]


def test_fail_soft_when_list_entries_fails(post_deploy, monkeypatch):
    calls = _wire(
        post_deploy,
        monkeypatch,
        entries=(0, None),
        flow_start=(200, {"flow_id": "X"}),
        flow_submit=(200, {"type": "create_entry"}),
    )
    assert (
        post_deploy.ensure_ha_jellyfin_integration("tok", "http://h:8096", "sol", "pw")
        is False
    )
    assert calls["posts"] == []


def test_fail_soft_when_flow_start_fails(post_deploy, monkeypatch):
    calls = _wire(
        post_deploy,
        monkeypatch,
        entries=(200, []),
        flow_start=(0, None),
        flow_submit=(200, {"type": "create_entry"}),
    )
    assert (
        post_deploy.ensure_ha_jellyfin_integration("tok", "http://h:8096", "sol", "pw")
        is False
    )
    # Started but got no flow_id → never submits the step, nothing to abort.
    assert calls["posts"] == [
        ("/api/config/config_entries/flow", {"handler": "jellyfin"})
    ]
    assert calls["deletes"] == []

"""Tests for the CalDAV READ-ingest wiring into the engine pod env (#524).

Option (a): point the read ingest at the resident's OWN `/<uid>/solaris/`
collection — the one collection `solaris` may legally read under the from_file
rights — reusing CALDAV_USERNAME=solaris + the managed DAV password. The
renderer prunes the template.yml CALDAV_* entries (leaving CALDAV_URL as the
empty anchor), so `_patched_caldav_read_yaml` PATCHES CALDAV_URL in place and
patches/inserts CALDAV_USERNAME/PASSWORD. Both paths must produce valid YAML
(a bad edit crash-loops the pod) and be idempotent. CARDDAV stays untouched.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

# PyYAML isn't installed in the templates CI env; the yaml-validity assertions
# run where it IS present (locally + the pod image at runtime). Skip the module
# when yaml is absent rather than fail collection.
yaml = pytest.importorskip("yaml")

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
    return _load("solaris_pd_caldav_read", TEMPLATES / "solaris" / "post-deploy.py")


# The shape seen on the box: CALDAV_URL renders as the empty anchor, USERNAME +
# PASSWORD pruned entirely, and CARDDAV present (must be left untouched).
_POD = (
    "apiVersion: v1\n"
    "kind: Pod\n"
    "spec:\n"
    "  containers:\n"
    "  - name: chat\n"
    "    env:\n"
    "    - name: CALDAV_URL\n"
    '      value: ""\n'
    "    - name: CARDDAV_URL\n"
    '      value: ""\n'
)

# The re-render case: CALDAV_URL/USERNAME/PASSWORD all present (empty).
_POD_WITH_ENTRIES = (
    "apiVersion: v1\n"
    "kind: Pod\n"
    "spec:\n"
    "  containers:\n"
    "  - name: chat\n"
    "    env:\n"
    "    - name: CALDAV_URL\n"
    '      value: ""\n'
    "    - name: CALDAV_USERNAME\n"
    '      value: ""\n'
    "    - name: CALDAV_PASSWORD\n"
    '      value: ""\n'
    "    - name: CARDDAV_URL\n"
    '      value: ""\n'
)


def _env_map(text: str) -> dict[str, str]:
    doc = yaml.safe_load(text)
    env = doc["spec"]["containers"][0]["env"]
    return {e["name"]: e["value"] for e in env}


def test_patches_url_and_inserts_creds(pd, monkeypatch):
    monkeypatch.delenv("DEADLINES_SYNC_URL_BASE", raising=False)
    new, n = pd._patched_caldav_read_yaml(_POD, "mdopp", "the-managed-pw")
    assert n == 1
    env = _env_map(new)  # valid YAML
    base = pd.DEADLINES_SYNC_URL_BASE_DEFAULT.rstrip("/")
    assert env["CALDAV_URL"] == f"{base}/mdopp/solaris/"
    assert env["CALDAV_USERNAME"] == "solaris"
    assert env["CALDAV_PASSWORD"] == "the-managed-pw"
    # CARDDAV read stays OFF — solaris has no legal contacts collection.
    assert env["CARDDAV_URL"] == ""


def test_idempotent(pd, monkeypatch):
    monkeypatch.delenv("DEADLINES_SYNC_URL_BASE", raising=False)
    once, _ = pd._patched_caldav_read_yaml(_POD, "mdopp", "pw")
    twice, n = pd._patched_caldav_read_yaml(once, "mdopp", "pw")
    assert twice == once
    assert n == 1
    assert twice.count("- name: CALDAV_USERNAME") == 1
    assert twice.count("- name: CALDAV_PASSWORD") == 1


def test_patch_in_place_when_entries_present(pd, monkeypatch):
    monkeypatch.delenv("DEADLINES_SYNC_URL_BASE", raising=False)
    new, n = pd._patched_caldav_read_yaml(_POD_WITH_ENTRIES, "mdopp", "new-pw")
    assert n == 1
    env = _env_map(new)
    assert env["CALDAV_PASSWORD"] == "new-pw"
    assert new.count("- name: CALDAV_PASSWORD") == 1


def test_url_base_env_override(pd, monkeypatch):
    monkeypatch.setenv("DEADLINES_SYNC_URL_BASE", "http://radicale.local:5232/")
    new, _ = pd._patched_caldav_read_yaml(_POD, "mdopp", "pw")
    assert _env_map(new)["CALDAV_URL"] == "http://radicale.local:5232/mdopp/solaris/"


def test_no_anchor_leaves_file_untouched(pd, monkeypatch):
    monkeypatch.delenv("DEADLINES_SYNC_URL_BASE", raising=False)
    no_anchor = "apiVersion: v1\nkind: Pod\nspec:\n  containers:\n  - name: chat\n"
    new, n = pd._patched_caldav_read_yaml(no_anchor, "mdopp", "pw")
    assert n == 0
    assert new == no_anchor


def test_apply_uses_household_uid(pd, tmp_path, monkeypatch):
    monkeypatch.delenv("DEADLINES_SYNC_URL_BASE", raising=False)
    (tmp_path / "solarisbay").mkdir()
    (tmp_path / "solarisbay" / pd.HOUSEHOLD_CALENDAR_UID_FILE).write_text(
        "mdopp\n", encoding="utf-8"
    )
    pod_yml = tmp_path / "solaris.yml"
    pod_yml.write_text(_POD, encoding="utf-8")
    monkeypatch.setattr(pd.os.path, "expanduser", lambda _p: str(pod_yml))
    assert pd.apply_caldav_read_to_engine(str(tmp_path), "mp") is True
    env = _env_map(pod_yml.read_text(encoding="utf-8"))
    base = pd.DEADLINES_SYNC_URL_BASE_DEFAULT.rstrip("/")
    assert env["CALDAV_URL"] == f"{base}/mdopp/solaris/"
    assert env["CALDAV_PASSWORD"] == "mp"


def test_apply_falls_back_to_default_uid(pd, tmp_path, monkeypatch):
    monkeypatch.delenv("DEADLINES_SYNC_URL_BASE", raising=False)
    monkeypatch.setenv("DEFAULT_UID", "resident")
    pod_yml = tmp_path / "solaris.yml"
    pod_yml.write_text(_POD, encoding="utf-8")
    monkeypatch.setattr(pd.os.path, "expanduser", lambda _p: str(pod_yml))
    assert pd.apply_caldav_read_to_engine(str(tmp_path), "mp") is True
    base = pd.DEADLINES_SYNC_URL_BASE_DEFAULT.rstrip("/")
    env = _env_map(pod_yml.read_text(encoding="utf-8"))
    assert env["CALDAV_URL"] == f"{base}/resident/solaris/"


def test_apply_noop_without_uid_or_password(pd, tmp_path, monkeypatch):
    monkeypatch.delenv("DEFAULT_UID", raising=False)
    pod_yml = tmp_path / "solaris.yml"
    pod_yml.write_text(_POD, encoding="utf-8")
    monkeypatch.setattr(pd.os.path, "expanduser", lambda _p: str(pod_yml))
    assert pd.apply_caldav_read_to_engine(str(tmp_path), "") is False  # no password
    assert pd.apply_caldav_read_to_engine(str(tmp_path), "mp") is False  # no uid
    assert pod_yml.read_text(encoding="utf-8") == _POD

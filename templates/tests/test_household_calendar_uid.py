"""Tests for wiring HOUSEHOLD_CALENDAR_UID into the engine pod env (#1011).

ServiceBay's renderer prunes the `HOUSEHOLD_CALENDAR_UID` template.yml env entry
(like the SYNC_DAV block) and drops the install-variable override for a newly
added var, so the pod can't carry it. `apply_household_calendar_uid_to_engine`
persists the operator's choice to a file (seeded from the env when SB passes it)
and stamps it into the deployed solaris.yml itself — insert after the CALDAV_URL
anchor when absent, patch in place when present. These tests cover the seed +
persist, the file-fallback, the patch path, and the unconfigured no-op.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

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
    return _load("solaris_pd_household", TEMPLATES / "solaris" / "post-deploy.py")


# A pod snippet with the CALDAV_URL anchor but no HOUSEHOLD_CALENDAR_UID entry.
_POD = (
    "apiVersion: v1\n"
    "kind: Pod\n"
    "spec:\n"
    "  containers:\n"
    "  - name: chat\n"
    "    env:\n"
    "    - name: CALDAV_URL\n"
    '      value: "http://127.0.0.1:5232/dav/"\n'
    "    - name: CALDAV_USERNAME\n"
    '      value: "resident"\n'
)


def _env_map(text: str) -> dict[str, str]:
    doc = yaml.safe_load(text)
    env = doc["spec"]["containers"][0]["env"]
    return {e["name"]: e["value"] for e in env}


def _setup(pd, tmp_path, monkeypatch):
    """Point the pod path at a tmp solaris.yml seeded with _POD."""
    pod_yml = tmp_path / "solaris.yml"
    pod_yml.write_text(_POD, encoding="utf-8")
    monkeypatch.setattr(pd.os.path, "expanduser", lambda _p: str(pod_yml))
    return pod_yml


def test_env_seeds_persists_and_inserts(pd, tmp_path, monkeypatch):
    monkeypatch.setenv("HOUSEHOLD_CALENDAR_UID", "mdopp")
    pod_yml = _setup(pd, tmp_path, monkeypatch)
    assert pd.apply_household_calendar_uid_to_engine(str(tmp_path)) is True
    # Stamped into the pod env (valid YAML), inserted after the CALDAV anchor.
    assert (
        _env_map(pod_yml.read_text(encoding="utf-8"))["HOUSEHOLD_CALENDAR_UID"]
        == "mdopp"
    )
    # And persisted so a later render that drops the env still re-applies it.
    persisted = tmp_path / "solarisbay" / pd.HOUSEHOLD_CALENDAR_UID_FILE
    assert persisted.read_text(encoding="utf-8").strip() == "mdopp"


def test_reads_persisted_file_when_env_absent(pd, tmp_path, monkeypatch):
    monkeypatch.delenv("HOUSEHOLD_CALENDAR_UID", raising=False)
    (tmp_path / "solarisbay").mkdir()
    (tmp_path / "solarisbay" / pd.HOUSEHOLD_CALENDAR_UID_FILE).write_text(
        "lena\n", encoding="utf-8"
    )
    pod_yml = _setup(pd, tmp_path, monkeypatch)
    assert pd.apply_household_calendar_uid_to_engine(str(tmp_path)) is True
    assert (
        _env_map(pod_yml.read_text(encoding="utf-8"))["HOUSEHOLD_CALENDAR_UID"]
        == "lena"
    )


def test_patches_existing_entry(pd, tmp_path, monkeypatch):
    monkeypatch.setenv("HOUSEHOLD_CALENDAR_UID", "mdopp")
    pod_yml = tmp_path / "solaris.yml"
    pod_yml.write_text(
        _POD + '    - name: HOUSEHOLD_CALENDAR_UID\n      value: ""\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(pd.os.path, "expanduser", lambda _p: str(pod_yml))
    assert pd.apply_household_calendar_uid_to_engine(str(tmp_path)) is True
    text = pod_yml.read_text(encoding="utf-8")
    assert _env_map(text)["HOUSEHOLD_CALENDAR_UID"] == "mdopp"
    assert text.count("- name: HOUSEHOLD_CALENDAR_UID") == 1  # no duplicate


def test_unconfigured_is_noop(pd, tmp_path, monkeypatch):
    monkeypatch.delenv("HOUSEHOLD_CALENDAR_UID", raising=False)
    pod_yml = _setup(pd, tmp_path, monkeypatch)
    before = pod_yml.read_text(encoding="utf-8")
    assert pd.apply_household_calendar_uid_to_engine(str(tmp_path)) is False
    assert pod_yml.read_text(encoding="utf-8") == before

"""Tests for the Solilos SOUL.md provisioning in the hermes post-deploy.

write_soul_md installs the shipped Solilos soul over Hermes' stock default
(or when none exists) but never clobbers a customised soul. Since #311 the
shipped soul is read from the household skill pack's bind mount and written to
/opt/data/SOUL.md THROUGH the container (the hermes data dir is 0700, and the
ServiceBay deploy runner stages only .py/.env so the adjacent shipped SOUL.md
never reached the post-deploy's host staging dir). The post-deploy has a
hyphenated filename under templates/, so it's loaded via importlib (same
pattern as test_sb_mcp_token.py).
"""

from __future__ import annotations

import hashlib
import importlib.util
import pathlib
import sys

import pytest

TEMPLATES = pathlib.Path(__file__).resolve().parents[1]
SHIPPED_SOUL = (TEMPLATES / "solilos" / "SOUL.md").read_text(encoding="utf-8")
SHIPPED_SHA = hashlib.sha256(SHIPPED_SOUL.encode("utf-8")).hexdigest()

# Container paths the post-deploy reads/writes (in-container, via podman exec).
SHIPPED_SOURCE = "/opt/data/skills/solilos/SOUL.md"
TARGET = "/opt/data/SOUL.md"
MARKER = "/opt/data/.soul.shipped.sha256"


def _load(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def hermes():
    return _load("hermes_post_deploy", TEMPLATES / "solilos" / "post-deploy.py")


class _FakeContainer:
    """In-memory stand-in for the hermes container's /opt/data, so the soul
    logic runs offline (no podman, no live container)."""

    def __init__(self):
        self.files: dict[str, str] = {}

    def write(self, path: str, content: str) -> bool:
        self.files[path] = content
        return True

    def read(self, path: str) -> str | None:
        return self.files.get(path)


@pytest.fixture
def fc(hermes, monkeypatch):
    c = _FakeContainer()
    # The household SOUL.md is bind-mounted into the container via the household
    # skill pack; seed it so write_soul_md can read the shipped soul.
    c.files[SHIPPED_SOURCE] = SHIPPED_SOUL
    monkeypatch.setattr(hermes, "write_file_in_container", c.write)
    monkeypatch.setattr(hermes, "read_file_in_container", c.read)
    return c


def test_writes_soul_when_absent(hermes, fc):
    assert hermes.write_soul_md() is True
    assert fc.files[TARGET] == SHIPPED_SOUL
    # Fresh install records the shipped-soul hash sidecar (#283).
    assert fc.files[MARKER].strip() == SHIPPED_SHA


def test_skips_when_shipped_soul_unreadable(hermes, fc):
    # The household pack bind mount missing (or empty) → skip, don't clobber.
    del fc.files[SHIPPED_SOURCE]
    assert hermes.write_soul_md() is False
    assert TARGET not in fc.files


def test_replaces_stock_default(hermes, fc):
    fc.files[TARGET] = "# Hermes Agent Persona\n\nstock default\n"
    assert hermes.write_soul_md() is True
    assert fc.files[TARGET] == SHIPPED_SOUL
    assert fc.files[MARKER].strip() == SHIPPED_SHA


def test_leaves_customised_soul_untouched(hermes, fc):
    custom = "# Our House\n\nA soul we wrote ourselves.\n"
    fc.files[TARGET] = custom
    assert hermes.write_soul_md() is False
    assert fc.files[TARGET] == custom


def test_idempotent_when_already_solilos(hermes, fc):
    fc.files[TARGET] = SHIPPED_SOUL
    assert hermes.write_soul_md() is False
    assert fc.files[TARGET] == SHIPPED_SOUL
    # An identical-but-marker-less soul (legacy / hand-applied) gets the
    # sidecar recorded so a future shipped change is recognised (#283).
    assert fc.files[MARKER].strip() == SHIPPED_SHA


def test_shipped_change_updates_unmodified_on_box_soul(hermes, fc):
    """#283: a shipped-soul change lands on an existing install whose on-box
    soul still matches the previously-recorded shipped hash (operator never
    edited it) — no manual podman exec needed on redeploy."""
    prior = "# Solilos — Soul\n\nan earlier shipped soul\n"
    prior_sha = hashlib.sha256(prior.encode("utf-8")).hexdigest()
    fc.files[TARGET] = prior
    fc.files[MARKER] = prior_sha + "\n"

    assert hermes.write_soul_md() is True
    assert fc.files[TARGET] == SHIPPED_SOUL
    assert fc.files[MARKER].strip() == SHIPPED_SHA


def test_operator_edited_soul_preserved_across_shipped_change(hermes, fc):
    """#283: an operator-edited soul (on-box hash != recorded shipped hash) is
    PRESERVED even when the shipped soul changed — operator edits never get
    clobbered."""
    prior_shipped_sha = hashlib.sha256(b"some earlier shipped soul").hexdigest()
    fc.files[MARKER] = prior_shipped_sha + "\n"
    edited = "# Solilos — Soul\n\nthe operator hand-tuned this voice\n"
    fc.files[TARGET] = edited

    assert hermes.write_soul_md() is False
    assert fc.files[TARGET] == edited
    # The sidecar is left as-is (still the prior shipped hash).
    assert fc.files[MARKER].strip() == prior_shipped_sha


def test_soul_grounds_device_questions_in_live_ha_tools():
    """#276: device/state questions must be answered from a live HA tool call,
    not from memory or an earlier turn — the shipped soul carries that rule."""
    soul = SHIPPED_SOUL.lower()
    assert "ha_list_entities" in soul
    assert "ha_get_state" in soul
    assert "never in memory" in soul

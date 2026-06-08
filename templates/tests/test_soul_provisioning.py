"""Tests for the Solilos SOUL.md provisioning in the hermes post-deploy.

write_soul_md installs the shipped Solilos soul over Hermes' stock default
(or when none exists) but never clobbers a customised soul. The post-deploy
has a hyphenated filename under templates/, so it's loaded via importlib
(same pattern as test_sb_mcp_token.py).
"""

from __future__ import annotations

import hashlib
import importlib.util
import pathlib
import sys

import pytest

TEMPLATES = pathlib.Path(__file__).resolve().parents[1]
SHIPPED_SOUL = (TEMPLATES / "hermes" / "SOUL.md").read_text(encoding="utf-8")
SHIPPED_SHA = hashlib.sha256(SHIPPED_SOUL.encode("utf-8")).hexdigest()


def _load(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def hermes():
    return _load("hermes_post_deploy", TEMPLATES / "hermes" / "post-deploy.py")


def _soul_path(data_dir: pathlib.Path) -> pathlib.Path:
    return data_dir / "hermes" / "SOUL.md"


def _marker_path(data_dir: pathlib.Path) -> pathlib.Path:
    return data_dir / "hermes" / ".soul.shipped.sha256"


def test_writes_soul_when_absent(hermes, tmp_path):
    assert hermes.write_soul_md(str(tmp_path)) is True
    assert _soul_path(tmp_path).read_text(encoding="utf-8") == SHIPPED_SOUL
    # Fresh install records the shipped-soul hash sidecar (#283).
    assert _marker_path(tmp_path).read_text(encoding="utf-8").strip() == SHIPPED_SHA


def test_replaces_stock_default(hermes, tmp_path):
    p = _soul_path(tmp_path)
    p.parent.mkdir(parents=True)
    p.write_text("# Hermes Agent Persona\n\nstock default\n", encoding="utf-8")
    assert hermes.write_soul_md(str(tmp_path)) is True
    assert p.read_text(encoding="utf-8") == SHIPPED_SOUL
    assert _marker_path(tmp_path).read_text(encoding="utf-8").strip() == SHIPPED_SHA


def test_leaves_customised_soul_untouched(hermes, tmp_path):
    p = _soul_path(tmp_path)
    p.parent.mkdir(parents=True)
    custom = "# Our House\n\nA soul we wrote ourselves.\n"
    p.write_text(custom, encoding="utf-8")
    assert hermes.write_soul_md(str(tmp_path)) is False
    assert p.read_text(encoding="utf-8") == custom


def test_idempotent_when_already_solilos(hermes, tmp_path):
    p = _soul_path(tmp_path)
    p.parent.mkdir(parents=True)
    p.write_text(SHIPPED_SOUL, encoding="utf-8")
    assert hermes.write_soul_md(str(tmp_path)) is False
    assert p.read_text(encoding="utf-8") == SHIPPED_SOUL
    # An identical-but-marker-less soul (legacy / hand-applied) gets the
    # sidecar recorded so a future shipped change is recognised (#283).
    assert _marker_path(tmp_path).read_text(encoding="utf-8").strip() == SHIPPED_SHA


def test_shipped_change_updates_unmodified_on_box_soul(hermes, tmp_path):
    """#283: a shipped-soul change lands on an existing install whose on-box
    soul still matches the previously-recorded shipped hash (operator never
    edited it) — no manual podman exec needed on redeploy."""
    p = _soul_path(tmp_path)
    p.parent.mkdir(parents=True)
    # Simulate an existing install whose on-box soul is a *prior* shipped
    # soul, recorded as such in the sidecar.
    prior = "# Solilos — Soul\n\nan earlier shipped soul\n"
    prior_sha = hashlib.sha256(prior.encode("utf-8")).hexdigest()
    p.write_text(prior, encoding="utf-8")
    _marker_path(tmp_path).write_text(prior_sha + "\n", encoding="utf-8")

    assert hermes.write_soul_md(str(tmp_path)) is True
    assert p.read_text(encoding="utf-8") == SHIPPED_SOUL
    assert _marker_path(tmp_path).read_text(encoding="utf-8").strip() == SHIPPED_SHA


def test_operator_edited_soul_preserved_across_shipped_change(hermes, tmp_path):
    """#283: an operator-edited soul (on-box hash != recorded shipped hash) is
    PRESERVED even when the shipped soul changed — operator edits never get
    clobbered."""
    p = _soul_path(tmp_path)
    p.parent.mkdir(parents=True)
    # The sidecar records a *prior* shipped hash; the on-box file is the
    # operator's own edit (hash differs from the recorded shipped one).
    prior_shipped_sha = hashlib.sha256(b"some earlier shipped soul").hexdigest()
    _marker_path(tmp_path).write_text(prior_shipped_sha + "\n", encoding="utf-8")
    edited = "# Solilos — Soul\n\nthe operator hand-tuned this voice\n"
    p.write_text(edited, encoding="utf-8")

    assert hermes.write_soul_md(str(tmp_path)) is False
    assert p.read_text(encoding="utf-8") == edited
    # The sidecar is left as-is (still the prior shipped hash).
    assert (
        _marker_path(tmp_path).read_text(encoding="utf-8").strip() == prior_shipped_sha
    )


def test_soul_grounds_device_questions_in_live_ha_tools():
    """#276: device/state questions must be answered from a live HA tool call,
    not from memory or an earlier turn — the shipped soul carries that rule."""
    soul = SHIPPED_SOUL.lower()
    assert "ha_list_entities" in soul
    assert "ha_get_state" in soul
    assert "never in memory" in soul

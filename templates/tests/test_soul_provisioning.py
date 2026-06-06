"""Tests for the Solilos SOUL.md provisioning in the hermes post-deploy.

write_soul_md installs the shipped Solilos soul over Hermes' stock default
(or when none exists) but never clobbers a customised soul. The post-deploy
has a hyphenated filename under templates/, so it's loaded via importlib
(same pattern as test_sb_mcp_token.py).
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

TEMPLATES = pathlib.Path(__file__).resolve().parents[1]
SHIPPED_SOUL = (TEMPLATES / "hermes" / "SOUL.md").read_text(encoding="utf-8")


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


def test_writes_soul_when_absent(hermes, tmp_path):
    assert hermes.write_soul_md(str(tmp_path)) is True
    assert _soul_path(tmp_path).read_text(encoding="utf-8") == SHIPPED_SOUL


def test_replaces_stock_default(hermes, tmp_path):
    p = _soul_path(tmp_path)
    p.parent.mkdir(parents=True)
    p.write_text("# Hermes Agent Persona\n\nstock default\n", encoding="utf-8")
    assert hermes.write_soul_md(str(tmp_path)) is True
    assert p.read_text(encoding="utf-8") == SHIPPED_SOUL


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

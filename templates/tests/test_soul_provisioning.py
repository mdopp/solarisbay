"""Tests for write_engine_soul — the SOUL.md seed/sync on the solilos-data
volume (#283 guard semantics, host-side file IO)."""

from __future__ import annotations

import hashlib
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
    return _load("solilos_pd_soul", TEMPLATES / "solilos" / "post-deploy.py")


@pytest.fixture
def data_dir(tmp_path):
    source = tmp_path / "solilos" / "skills" / "household" / "SOUL.md"
    source.parent.mkdir(parents=True)
    source.write_text("Ich bin Sol.\n", encoding="utf-8")
    (tmp_path / "solbay").mkdir()
    return tmp_path


def _target(data_dir):
    return data_dir / "solbay" / "SOUL.md"


def test_writes_soul_when_absent(pd, data_dir):
    assert pd.write_engine_soul(str(data_dir)) is True
    assert _target(data_dir).read_text(encoding="utf-8") == "Ich bin Sol.\n"
    marker = data_dir / "solbay" / ".soul.shipped.sha256"
    assert marker.read_text().strip() == hashlib.sha256(b"Ich bin Sol.\n").hexdigest()


def test_noop_when_identical(pd, data_dir):
    pd.write_engine_soul(str(data_dir))
    assert pd.write_engine_soul(str(data_dir)) is False


def test_shipped_change_updates_unmodified_soul(pd, data_dir):
    pd.write_engine_soul(str(data_dir))
    source = data_dir / "solilos" / "skills" / "household" / "SOUL.md"
    source.write_text("Ich bin Sol v2.\n", encoding="utf-8")
    assert pd.write_engine_soul(str(data_dir)) is True
    assert _target(data_dir).read_text(encoding="utf-8") == "Ich bin Sol v2.\n"


def test_operator_edited_soul_preserved(pd, data_dir):
    pd.write_engine_soul(str(data_dir))
    _target(data_dir).write_text("Operator hat editiert.\n", encoding="utf-8")
    source = data_dir / "solilos" / "skills" / "household" / "SOUL.md"
    source.write_text("Ich bin Sol v3.\n", encoding="utf-8")
    assert pd.write_engine_soul(str(data_dir)) is False
    assert _target(data_dir).read_text(encoding="utf-8") == "Operator hat editiert.\n"


def test_skips_when_shipped_soul_unreadable(pd, tmp_path):
    (tmp_path / "solbay").mkdir()
    assert pd.write_engine_soul(str(tmp_path)) is False

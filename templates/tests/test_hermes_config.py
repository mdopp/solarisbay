"""Tests for hermes/post-deploy.py's config.yaml render: the household
agent's disabled_toolsets includes `kanban` (#268 TTFT trim) while keeping
`cronjob` ENABLED (load-bearing for timers/alarms/reminders + the 3 system
crons).

The hyphenated post-deploy.py is loaded via importlib; the network probes
(honcho /health, ollama /api/tags) are monkeypatched so the render runs
offline against a temp data dir.
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
def hermes():
    return _load("hermes_post_deploy", TEMPLATES / "solilos" / "post-deploy.py")


def _render(hermes, tmp_path, monkeypatch) -> str:
    monkeypatch.setattr(hermes, "detect_honcho", lambda port: False)
    monkeypatch.setattr(hermes, "enumerate_ollama_tags", lambda url: [])
    path = hermes.write_config_yaml(
        str(tmp_path), "http://127.0.0.1:11434/v1", "gemma4:12b"
    )
    assert path is not None
    return pathlib.Path(path).read_text(encoding="utf-8")


def test_kanban_is_disabled(hermes, tmp_path, monkeypatch):
    content = _render(hermes, tmp_path, monkeypatch)
    assert "  disabled_toolsets:\n" in content
    assert "    - kanban\n" in content


def test_cronjob_stays_enabled(hermes, tmp_path, monkeypatch):
    """cronjob is load-bearing (reminders/timers + the 3 system crons) — it
    must NOT appear in disabled_toolsets."""
    content = _render(hermes, tmp_path, monkeypatch)
    # Each disabled toolset is a "    - <name>\n" line; cronjob must not be one.
    assert "    - cronjob\n" not in content

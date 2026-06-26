"""JELLYFIN_CAST_URL config resolution (#604).

The stream URL a Cast device fetches must use a LAN-reachable base, distinct
from the engine's localhost JELLYFIN_URL. When JELLYFIN_CAST_URL is unset it
falls back to JELLYFIN_URL (safe no-op = the prior behavior).
"""

from __future__ import annotations

import pytest

from solaris_chat.config import Settings


@pytest.fixture
def clean_env(monkeypatch):
    for key in ("JELLYFIN_URL", "JELLYFIN_CAST_URL"):
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


def test_cast_url_defaults_to_jellyfin_url_when_unset(clean_env):
    clean_env.setenv("JELLYFIN_URL", "http://127.0.0.1:8096")
    settings = Settings.from_env()
    assert settings.jellyfin_cast_url == "http://127.0.0.1:8096"


def test_cast_url_overrides_when_set(clean_env):
    clean_env.setenv("JELLYFIN_URL", "http://127.0.0.1:8096")
    clean_env.setenv("JELLYFIN_CAST_URL", "http://192.168.178.100:8096")
    settings = Settings.from_env()
    assert settings.jellyfin_url == "http://127.0.0.1:8096"
    assert settings.jellyfin_cast_url == "http://192.168.178.100:8096"


def test_cast_url_empty_falls_back_to_jellyfin_url(clean_env):
    clean_env.setenv("JELLYFIN_URL", "http://127.0.0.1:8096")
    clean_env.setenv("JELLYFIN_CAST_URL", "   ")
    settings = Settings.from_env()
    assert settings.jellyfin_cast_url == "http://127.0.0.1:8096"

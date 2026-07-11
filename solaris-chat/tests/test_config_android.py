"""Android TWA / Digital Asset Links config parsing (#716).

ANDROID_PACKAGE defaults to the household app id; ANDROID_CERT_FINGERPRINTS is
a comma-separated, stripped list that defaults empty (the signing key doesn't
exist until the android repo scaffolds it).
"""

from __future__ import annotations

import pytest

from solaris_chat.config import Settings


@pytest.fixture
def clean_env(monkeypatch):
    for key in ("ANDROID_PACKAGE", "ANDROID_CERT_FINGERPRINTS"):
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


def test_defaults_when_unset(clean_env):
    settings = Settings.from_env()
    assert settings.android_package == "cloud.dopp.solaris"
    assert settings.android_cert_fingerprints == ()


def test_fingerprints_parsed_stripped(clean_env):
    clean_env.setenv("ANDROID_CERT_FINGERPRINTS", " AA:BB , CC:DD ,  ")
    settings = Settings.from_env()
    assert settings.android_cert_fingerprints == ("AA:BB", "CC:DD")


def test_package_override(clean_env):
    clean_env.setenv("ANDROID_PACKAGE", "com.example.app")
    settings = Settings.from_env()
    assert settings.android_package == "com.example.app"

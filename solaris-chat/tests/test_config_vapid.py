"""VAPID public-key derivation from the private key (#801).

A ServiceBay redeploy silently empties the non-secret VAPID_PUBLIC_KEY env, so
the engine derives it from VAPID_PRIVATE_KEY when unset instead of depending on
the env surviving. An explicit public key still wins; a missing/bad private key
leaves push disabled rather than crashing boot.
"""

from __future__ import annotations

import pytest

from solaris_chat.config import Settings

# A fixed P-256 keypair (private scalar 0x11..11). Both encodings py_vapid
# accepts and the matching uncompressed public point, base64url, no padding.
RAW_PRIV = "ERERERERERERERERERERERERERERERERERERERERERE"
DER_PRIV = "MIGHAgEAMBMGByqGSM49AgEGCCqGSM49AwEHBG0wawIBAQQgERERERERERERERERERERERERERERERERERERERERERGhRANCAAQCF-YX8LZEOSgnj5aZnmmiOk8sFSvfbWzfZuW4AoLU7RlKfevLl3EtLdo8qFqodlpW9F_HWFmWUvKJfGUwbleU"  # noqa: E501
EXPECTED_PUB = "BAIX5hfwtkQ5KCePlpmeaaI6TywVK99tbN9m5bgCgtTtGUp968uXcS0t2jyoWqh2Wlb0X8dYWZZS8ol8ZTBuV5Q"


@pytest.fixture
def clean_env(monkeypatch):
    for key in ("VAPID_PUBLIC_KEY", "VAPID_PRIVATE_KEY", "VAPID_SUBJECT"):
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


def test_derives_public_from_raw_private_when_public_unset(clean_env):
    clean_env.setenv("VAPID_PRIVATE_KEY", RAW_PRIV)
    settings = Settings.from_env()
    assert settings.vapid_public_key == EXPECTED_PUB


def test_derives_public_from_der_private_when_public_unset(clean_env):
    clean_env.setenv("VAPID_PRIVATE_KEY", DER_PRIV)
    settings = Settings.from_env()
    assert settings.vapid_public_key == EXPECTED_PUB


def test_explicit_public_key_is_used_as_is(clean_env):
    clean_env.setenv("VAPID_PRIVATE_KEY", RAW_PRIV)
    clean_env.setenv("VAPID_PUBLIC_KEY", "explicit-public-key")
    settings = Settings.from_env()
    assert settings.vapid_public_key == "explicit-public-key"


def test_no_private_key_leaves_push_disabled(clean_env):
    settings = Settings.from_env()
    assert settings.vapid_public_key == ""
    assert settings.vapid_private_key == ""


def test_malformed_private_key_does_not_crash(clean_env):
    clean_env.setenv("VAPID_PRIVATE_KEY", "not-a-valid-key")
    settings = Settings.from_env()
    assert settings.vapid_public_key == ""

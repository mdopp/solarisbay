"""Env-driven configuration for the chat proxy.

The proxy serves a static page and forwards turns to Hermes' native
session API. It holds API_SERVER_KEY server-side and maps the Authelia
trusted-proxy identity header to a Hermes uid.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    hermes_url: str
    hermes_token: str
    remote_user_header: str
    default_uid: str

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            host=os.environ.get("CHAT_HOST", "127.0.0.1"),
            port=int(os.environ.get("CHAT_PORT", "8787")),
            hermes_url=os.environ.get("HERMES_URL", "http://127.0.0.1:8642"),
            hermes_token=os.environ.get("API_SERVER_KEY", ""),
            # Authelia forwards the authenticated identity on this header
            # via the trusted reverse proxy. We never trust it from an
            # untrusted source: the pod binds loopback and only NPM
            # (which sets the header after Authelia) can reach it.
            remote_user_header=os.environ.get("REMOTE_USER_HEADER", "Remote-User"),
            # Fallback uid when the header is absent (e.g. offline test
            # access straight to the loopback port, no Authelia in front).
            default_uid=os.environ.get("DEFAULT_UID", "household"),
        )


settings = Settings.from_env()

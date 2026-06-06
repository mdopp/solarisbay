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
    remote_groups_header: str
    admin_group: str
    default_uid: str
    skills_dir: str
    soul_path: str
    config_agent_url: str
    logout_url: str

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
            # Authelia also forwards the user's groups (comma-separated) on
            # this header. Panel writes (skills/soul/model — phase 2) gate on
            # membership of `admin_group`; same trusted-proxy trust as above.
            remote_groups_header=os.environ.get(
                "REMOTE_GROUPS_HEADER", "Remote-Groups"
            ),
            admin_group=os.environ.get("ADMIN_GROUP", "admins"),
            # Fallback uid when the header is absent (e.g. offline test
            # access straight to the loopback port, no Authelia in front).
            default_uid=os.environ.get("DEFAULT_UID", "household"),
            # Read-only bind mount of the Solilos skill pack (host
            # solbay/skills) and the global SOUL.md — the panel renders both
            # from disk because Hermes exposes no body/soul API.
            skills_dir=os.environ.get("SKILLS_DIR", "/data/skills"),
            soul_path=os.environ.get("SOUL_PATH", "/data/SOUL.md"),
            # The privileged config sidecar inside the hermes pod (loopback);
            # the panel proxies admin soul writes here because the chat pod
            # can't write Hermes' own data dir. Auth reuses API_SERVER_KEY.
            config_agent_url=os.environ.get(
                "CONFIG_AGENT_URL", "http://127.0.0.1:8650"
            ),
            # Optional Authelia logout URL for the sidebar footer. Empty ⇒ the
            # panel hides the logout link (avoids a dead link when unset).
            logout_url=os.environ.get("LOGOUT_URL", ""),
        )


settings = Settings.from_env()

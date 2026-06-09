"""Env-driven configuration for the chat proxy.

The proxy serves a static page and forwards turns to Hermes' native
session API. It holds API_SERVER_KEY server-side and maps the Authelia
trusted-proxy identity header to a Hermes uid.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from solilos_chat import context


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    hermes_url: str
    hermes_admin_url: str
    hermes_token: str
    remote_user_header: str
    remote_groups_header: str
    admin_group: str
    default_uid: str
    skills_dir: str
    soul_path: str
    config_agent_url: str
    logout_url: str
    context_window_override: int | None
    ollama_url: str
    compaction_threshold: float
    attachments_dir: str
    frame_ancestors: str
    fast_model: str
    thorough_model: str
    solilos_db_path: str
    notes_dir: str

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            host=os.environ.get("CHAT_HOST", "127.0.0.1"),
            port=int(os.environ.get("CHAT_PORT", "8787")),
            hermes_url=os.environ.get("HERMES_URL", "http://127.0.0.1:8642"),
            # The admin/maintenance Hermes gateway (#293): a second instance
            # running the `admin` profile (12b + servicebay_admin MCP), reached
            # only by the admin-gated servicebay-maintenance path. The household
            # gateway (HERMES_URL, :8642) serves every resident session.
            hermes_admin_url=os.environ.get(
                "HERMES_ADMIN_URL", "http://127.0.0.1:8643"
            ),
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
            # Context window (tokens): empty/"auto" => derive from the live
            # Ollama active model at runtime (#235), so the compaction cap always
            # matches what the model is actually loaded with and adapts per
            # model. A positive integer here is an explicit operator OVERRIDE
            # that wins over the derived value (ops control). Resolved at boot +
            # refreshed periodically; see context.derive_context_window.
            context_window_override=context.parse_override(
                os.environ.get("CONTEXT_WINDOW")
            ),
            # Where Ollama's API lives (host loopback — the chat pod is
            # hostNetwork). Queried for the active model's loaded context window.
            ollama_url=os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434"),
            # Fraction of the context window at which a chat is auto-compacted
            # (#210): extract durable learnings to memory, then continue in a
            # fresh small-context session. ~0.90 leaves headroom so a turn never
            # truncates while the two compaction turns run.
            compaction_threshold=float(os.environ.get("COMPACTION_THRESHOLD", "0.90")),
            # Host-mounted dir where the proxy persists image attachments
            # Hermes drops (the one stateful store, #202).
            attachments_dir=os.environ.get("ATTACHMENTS_DIR", "/data/attachments"),
            # CSP `frame-ancestors` source list — who may iframe the chat
            # (#228). Default `'self'`; the ServiceBay maintenance embed sets
            # `'self' https://admin.dopp.cloud` so admin.dopp.cloud can frame it.
            frame_ancestors=os.environ.get("FRAME_ANCESTORS", "'self'"),
            # Adaptive model routing (latency bundle). The model is bound at
            # Hermes session create from the chosen reasoning effort: a
            # Schnell/FAST conversation → FAST_MODEL (gemma4:e2b, ~4× faster
            # prefill + reliable HA tool-calls); a Gründlich/thorough one →
            # THOROUGH_MODEL (gemma4:12b). Empty (default) => no per-session
            # override, Hermes' configured model.model is used — so routing is
            # off until the operator sets the tags (minimal-knobs, opt-in).
            fast_model=os.environ.get("FAST_MODEL", "").strip(),
            thorough_model=os.environ.get("THOROUGH_MODEL", "").strip(),
            # solilos.db (bind-mounted into the pod) holds the topics registry
            # and chat<->topic assignments (#241). Same path the gatekeeper and
            # schema-init sidecar use.
            solilos_db_path=os.environ.get(
                "SOLILOS_DB_PATH", "/var/lib/solilos/solilos.db"
            ),
            # The Obsidian notes vault (Syncthing-synced), read for the
            # topic dashboard's per-topic note list (#244). Same path the
            # ingestion/notes-search skills use inside the Hermes runtime.
            notes_dir=os.environ.get("NOTES_DIR", "/opt/data/notes"),
        )


settings = Settings.from_env()

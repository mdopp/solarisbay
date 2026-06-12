"""Env-driven configuration for the Sol Engine chat server.

One process owns the agent loop, the chat surface, the Ollama facade for
HA Assist, the timer scheduler and the night crons. It maps the Authelia
trusted-proxy identity header to a resident uid and holds the API key
server-side.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from solilos_chat import context


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    api_key: str
    remote_user_header: str
    remote_groups_header: str
    admin_group: str
    default_uid: str
    skills_dir: str
    soul_path: str
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
    hass_url: str
    hass_token: str
    tavily_api_key: str
    admin_soul_path: str
    admin_skills_dir: str
    sb_mcp_url: str
    sb_mcp_token_path: str

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            host=os.environ.get("CHAT_HOST", "127.0.0.1"),
            port=int(os.environ.get("CHAT_PORT", "8787")),
            # Server-side bearer: the HA conversation agent and the
            # voice-gatekeeper present it on the Ollama facade. Empty leaves
            # the facade open — acceptable only on the loopback-only bind.
            api_key=os.environ.get("SOL_API_KEY", ""),
            # Authelia forwards the authenticated identity on this header
            # via the trusted reverse proxy. We never trust it from an
            # untrusted source: the pod binds loopback and only NPM
            # (which sets the header after Authelia) can reach it.
            remote_user_header=os.environ.get("REMOTE_USER_HEADER", "Remote-User"),
            # Authelia also forwards the user's groups (comma-separated) on
            # this header. Panel writes (skills/soul/model) gate on
            # membership of `admin_group`; same trusted-proxy trust as above.
            remote_groups_header=os.environ.get(
                "REMOTE_GROUPS_HEADER", "Remote-Groups"
            ),
            admin_group=os.environ.get("ADMIN_GROUP", "admins"),
            # Fallback uid when the header is absent (e.g. offline test
            # access straight to the loopback port, no Authelia in front).
            default_uid=os.environ.get("DEFAULT_UID", "household"),
            # The Solilos skill pack (host solbay/skills) — the panel renders
            # and edits it, and the engine reads cron-job skill bodies from it.
            skills_dir=os.environ.get("SKILLS_DIR", "/data/skills"),
            soul_path=os.environ.get("SOUL_PATH", "/var/lib/solilos/SOUL.md"),
            # Optional Authelia logout URL for the sidebar footer. Empty ⇒ the
            # panel hides the logout link (avoids a dead link when unset).
            logout_url=os.environ.get("LOGOUT_URL", ""),
            # Context window (tokens): empty/"auto" => derive from the live
            # Ollama active model at runtime (#235), so the compaction cap always
            # matches what the model is actually loaded with and adapts per
            # model. A positive integer here is an explicit operator OVERRIDE
            # that wins over the derived value (ops control).
            context_window_override=context.parse_override(
                os.environ.get("CONTEXT_WINDOW")
            ),
            # Where Ollama's API lives (host loopback — the chat pod is
            # hostNetwork). The engine's only LLM backend.
            ollama_url=os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434"),
            # Fraction of the context window at which a chat is auto-compacted
            # (#210): extract durable learnings to memory, then continue in a
            # fresh small-context session. ~0.90 leaves headroom so a turn never
            # truncates while the two compaction turns run.
            compaction_threshold=float(os.environ.get("COMPACTION_THRESHOLD", "0.90")),
            # Host-mounted dir where the proxy persists image attachments (#202).
            attachments_dir=os.environ.get("ATTACHMENTS_DIR", "/data/attachments"),
            # CSP `frame-ancestors` source list — who may iframe the chat
            # (#228). Default `'self'`; the ServiceBay maintenance embed sets
            # `'self' https://admin.dopp.cloud` so admin.dopp.cloud can frame it.
            frame_ancestors=os.environ.get("FRAME_ANCESTORS", "'self'"),
            # The engine model map: the household/voice hot path runs the fast
            # model; "Gründlich" chats, the admin persona and the night crons
            # run the thorough one. Box-benched 2026-06-12 (e2b vs e4b vs 12b).
            fast_model=os.environ.get("FAST_MODEL", "gemma4:e2b").strip(),
            thorough_model=os.environ.get("THOROUGH_MODEL", "gemma4:12b").strip(),
            # solilos.db (bind-mounted into the pod) holds the engine sessions,
            # timers, cron stamps, topics and traces. Same path the gatekeeper
            # and schema-init sidecar use.
            solilos_db_path=os.environ.get(
                "SOLILOS_DB_PATH", "/var/lib/solilos/solilos.db"
            ),
            # The Obsidian notes vault (Syncthing-synced) — the engine's notes
            # tools and the topic dashboard read/write here.
            notes_dir=os.environ.get("NOTES_DIR", "/opt/data/notes"),
            # The Sol Engine's direct Home Assistant access: device control
            # tools + the prompt-injected entity registry + timer announce.
            hass_url=os.environ.get("HASS_URL", "").strip(),
            hass_token=os.environ.get("HASS_TOKEN", "").strip(),
            # Web search backend. Empty => the keyless ddgs backend.
            tavily_api_key=os.environ.get("TAVILY_API_KEY", "").strip(),
            # The operator persona's soul for the admin profile; falls back to
            # the household soul when unset.
            admin_soul_path=os.environ.get("ADMIN_SOUL_PATH", "").strip(),
            # The operator skill pack, folded into the admin profile's prompt.
            admin_skills_dir=os.environ.get("ADMIN_SKILLS_DIR", "").strip(),
            # The servicebay_admin MCP endpoint + the token file the
            # post-deploy mints (read+lifecycle+mutate; no destroy/exec).
            sb_mcp_url=os.environ.get("SB_MCP_URL", "").strip(),
            sb_mcp_token_path=os.environ.get(
                "SB_MCP_TOKEN_PATH", "/var/lib/solilos/sb-admin-token"
            ),
        )


settings = Settings.from_env()

"""Env-driven configuration for the gatekeeper.

Phase 0 (initial release) kept this tiny. Phase 2 (#937) adds the
SpeechBrain ECAPA model toggle + threshold and the solilos.db path for
the voice_embeddings table.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Settings:
    gatekeeper_uri: str
    whisper_uri: str
    piper_uri: str
    openwakeword_uri: str
    hermes_url: str
    hermes_token: str
    default_uid: str
    push_host: str
    push_port: int
    push_token: str
    mcp_host: str
    mcp_port: int
    mcp_token: str
    solilos_db_path: str
    speaker_id_enabled: bool
    speaker_id_threshold: float
    voice_pe_devices: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> "Settings":
        raw_devices = os.environ.get("VOICE_PE_DEVICES", "")
        devices: dict[str, str] = {}
        if raw_devices.strip():
            try:
                parsed = json.loads(raw_devices)
                if isinstance(parsed, dict):
                    devices = {str(k): str(v) for k, v in parsed.items()}
            except json.JSONDecodeError:
                devices = {}
        flag = os.environ.get("SOLILOS_SPEAKER_ID_ENABLED", "").strip().lower()
        try:
            threshold = float(os.environ.get("SOLILOS_SPEAKER_ID_THRESHOLD", "0.55"))
        except ValueError:
            threshold = 0.55
        return cls(
            gatekeeper_uri=os.environ.get("GATEKEEPER_URI", "tcp://0.0.0.0:10700"),
            whisper_uri=os.environ.get("WHISPER_URI", "tcp://127.0.0.1:10300"),
            piper_uri=os.environ.get("PIPER_URI", "tcp://127.0.0.1:10200"),
            openwakeword_uri=os.environ.get(
                "OPENWAKEWORD_URI", "tcp://127.0.0.1:10400"
            ),
            hermes_url=os.environ["HERMES_URL"],
            hermes_token=os.environ.get("HERMES_TOKEN", ""),
            default_uid=os.environ.get("DEFAULT_UID", "michael"),
            # Loopback by default: the push + MCP listeners only ever serve
            # Hermes, which shares the host netns (hostNetwork) and reaches
            # them over 127.0.0.1. Binding 0.0.0.0 under hostNetwork would
            # expose them on the host's LAN interface, where an empty token
            # leaves them unauthenticated (#116). Only the Wyoming port
            # (GATEKEEPER_URI) needs the LAN, for satellites.
            push_host=os.environ.get("PUSH_HOST", "127.0.0.1"),
            push_port=int(os.environ.get("PUSH_PORT", "10750")),
            push_token=os.environ.get("PUSH_TOKEN", ""),
            mcp_host=os.environ.get("MCP_HOST", "127.0.0.1"),
            mcp_port=int(os.environ.get("MCP_PORT", "10760")),
            mcp_token=os.environ.get("GATEKEEPER_MCP_TOKEN", ""),
            solilos_db_path=os.environ.get(
                "SOLILOS_DB_PATH", "/var/lib/solilos/solilos.db"
            ),
            speaker_id_enabled=flag in {"1", "true", "yes", "on"},
            speaker_id_threshold=threshold,
            voice_pe_devices=devices,
        )


settings = Settings.from_env()

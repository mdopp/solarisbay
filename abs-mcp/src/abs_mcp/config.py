"""Env-driven configuration for the Audiobookshelf MCP shim."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    abs_base_url: str
    abs_api_key: str
    mcp_host: str
    mcp_port: int
    mcp_token: str

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            # Audiobookshelf reached over host loopback — the media pod
            # publishes :13378 on the host and this shim runs in a
            # hostNetwork pod, so they share the host netns.
            abs_base_url=os.environ.get(
                "ABS_BASE_URL", "http://127.0.0.1:13378"
            ).rstrip("/"),
            abs_api_key=os.environ.get("ABS_API_KEY", ""),
            # Loopback by default: only Hermes (same host netns) calls this
            # MCP server. Binding 0.0.0.0 under hostNetwork would put it on
            # the LAN where a blank token is unauthenticated (cf. #116).
            mcp_host=os.environ.get("MCP_HOST", "127.0.0.1"),
            mcp_port=int(os.environ.get("MCP_PORT", "10770")),
            mcp_token=os.environ.get("ABS_MCP_TOKEN", ""),
        )


settings = Settings.from_env()

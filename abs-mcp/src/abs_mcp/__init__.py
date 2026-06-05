"""Solilos Audiobookshelf MCP shim.

A thin, read-only bridge: it exposes the household's Audiobookshelf library
to Hermes as MCP tools (`abs_search`, `abs_availability`) backed by the
Audiobookshelf REST API. The ABS credential lives here in the shim's env,
never in a skill prompt — so a prompt-injected session can't reach it, and
the shim deliberately offers no write/destructive tools.
"""

from __future__ import annotations

__version__ = "0.1.0"

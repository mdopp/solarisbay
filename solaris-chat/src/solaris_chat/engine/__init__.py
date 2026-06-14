"""Solaris Engine — the native agent core that replaced Hermes.

One process owns the whole turn: prompt assembly (soul + HA entity registry),
the agent loop on Ollama `/api/chat`, lean hand-written tools, session storage
in solaris.db, and native LLM tracing. Model + thinking are per-turn request
parameters — the Hermes-era three-gateway construct collapses into three
in-process profiles sharing one store and one Ollama connection.
"""

from solaris_chat.engine.client import EngineClient, EngineProfile

__all__ = ["EngineClient", "EngineProfile"]

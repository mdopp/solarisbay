"""Tool registry for the Sol Engine.

Every tool is a hand-written, token-lean definition (~100-200 tokens) plus an
async handler. The Hermes-era 8.4k-token tool block is the single biggest
thing this engine exists to kill — keep definitions terse and resist
accumulating tools a profile doesn't need.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

Handler = Callable[[dict[str, Any]], Awaitable[str]]


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Handler

    def definition(self) -> dict[str, Any]:
        """The Ollama `tools` entry."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


class Toolbox:
    def __init__(self, tools: list[Tool]):
        self._tools = {t.name: t for t in tools}

    async def prepare(self) -> None:
        """Hook for toolboxes that fetch definitions remotely (MCP); awaited
        once per turn before `definitions()` is read. No-op here."""

    def definitions(self) -> list[dict[str, Any]]:
        return [t.definition() for t in self._tools.values()]

    async def dispatch(self, name: str, arguments: dict[str, Any]) -> str:
        tool = self._tools.get(name)
        if tool is None:
            return f'{{"error": "unknown tool: {name}"}}'
        try:
            return await tool.handler(arguments)
        except Exception as e:  # noqa: BLE001 — a tool error is model feedback,
            # not a turn-killer: the model sees it and can recover or apologize.
            return f'{{"error": "{type(e).__name__}: {str(e)[:200]}"}}'

    def names(self) -> list[str]:
        return list(self._tools)

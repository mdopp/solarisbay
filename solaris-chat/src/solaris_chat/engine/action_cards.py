"""Action-card handler registry (Wartung P2a, #787).

An action-card is a chat card `{kind:"action", title, body, buttons:[…]}` where
each button carries an `action_id`. A button press posts that id to
`/api/action-callback`; this module maps the id to the server-side handler that
runs it. It is the shared primitive the update-cards (#788) and approval-cards
(#790) build on — they register their own ids here.

A handler declares `destructive`: a destructive action must not fire on a bare
tap, so the endpoint routes it through the same confirm-gate the HA card taps
use (#702) — it runs only when the callback re-sends `confirmed=true`. A handler
declares `admin`: an admin-only action is refused for a non-admin caller
(#788/#789 SB-MCP deploy/exec register with `admin=True`). The registry keeps
the policy (which ids exist, which are destructive, which are admin-only) in one
place; the endpoint is a thin dispatcher over it.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

Handler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class ActionHandler:
    """A server-side action a card button triggers.

    `destructive` gates the action behind an explicit confirm (the callback must
    carry `confirmed=true`); `admin` gates it behind an admin caller (a non-admin
    resident is refused before the handler runs, so `confirmed=true` can't bypass
    it) — the SB-MCP deploy/exec handlers (#788/#789) register with `admin=True`.
    `run` receives the callback body and returns the JSON result surfaced to the
    client.
    """

    run: Handler
    destructive: bool = False
    admin: bool = False


async def _ping(_body: dict[str, Any]) -> dict[str, Any]:
    """A harmless demonstrable action — proves the callback→handler wiring."""
    return {"ok": True, "detail": "pong"}


_REGISTRY: dict[str, ActionHandler] = {
    "ping": ActionHandler(run=_ping),
}


def register(
    action_id: str,
    handler: Handler,
    *,
    destructive: bool = False,
    admin: bool = False,
) -> None:
    """Register a handler for an action-card button id (used by #788/#790)."""
    _REGISTRY[action_id] = ActionHandler(
        run=handler, destructive=destructive, admin=admin
    )


def get(action_id: str) -> ActionHandler | None:
    return _REGISTRY.get(action_id)

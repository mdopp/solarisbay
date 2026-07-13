"""One-shot elevated delete/exec escalation → Wartung approval-cards (P2c, #789).

The Wartung chat's ambient SB-MCP token is read+lifecycle+mutate — it CANNOT
delete/exec (#784 P1b). When an admin turn's SB-MCP call is refused for lack of
destroy/exec scope, `McpToolbox` (mcp_tools.py) does NOT run it: it asks SB-MCP's
`request_token` for a ONE-SHOT token bound to exactly that op (servicebay#2245 /
SB PR #2266) — which parks an owner approval and mints nothing — then calls the
sink here to inject an [Approve]/[Deny] card naming the exact op into the Wartung
chat (#785 `inject()` + the #787 action-card kind).

On the owner's **Approve** (server.py handler, admin=True + destructive=True), the
handler polls `poll_token_request` for the single-use, short-TTL token bound to
that op and runs it ONCE over a fresh connection carrying only that token —
`McpToolbox.run_one_shot`. The one-shot token never touches the ambient toolbox,
so the ambient token stays read+lifecycle+mutate: no standing elevation. **Deny**
runs nothing.
"""

from __future__ import annotations

from typing import Any

# The [Approve] / [Deny] button action ids, shared with the server-side handlers.
RUN_ACTION = "u789-run-approved-op"
DENY_ACTION = "u789-deny-op"


def op_label(tool_name: str, service: str | None) -> str:
    """Human one-liner naming the exact destructive op the card gates."""
    return f"{tool_name} auf „{service}“" if service else tool_name


def card(op: dict[str, Any], request_id: str) -> dict[str, Any]:
    """The #787 action-card offering [Approve]/[Deny] for one one-shot op (#789).

    Both buttons carry the op (`tool_name`, `service`, `arguments`) and the SB
    `request_id`, so the (admin-gated) handlers know exactly what to run and
    which one-shot token to collect. [Approve] runs the destructive op, so it is
    additionally confirm-gated by the endpoint; [Deny] runs nothing."""
    tool_name = str(op.get("tool_name") or "?")
    service = op.get("service")
    service = service if isinstance(service, str) and service else None
    params = {
        "request_id": request_id,
        "tool_name": tool_name,
        "service": service,
        "arguments": op.get("arguments") or {},
    }
    return {
        "kind": "action",
        "title": "Freigabe: destruktive Operation",
        "body": f"Der Wartung-Chat möchte {op_label(tool_name, service)} ausführen. "
        "Das läuft nur nach deiner Freigabe, mit einem einmaligen Token.",
        "buttons": [
            {
                "label": "Approve",
                "action_id": RUN_ACTION,
                "destructive": True,
                "params": params,
            },
            {
                "label": "Deny",
                "action_id": DENY_ACTION,
                "params": params,
            },
        ],
    }

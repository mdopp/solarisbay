---
name: solaris-admin-act
description: Change box state via the servicebay_admin MCP — lifecycle (start/stop/restart) and mutate (redeploy, config edit, proxy-route). Confirms impactful mutations first.
kind: skill
scope: admin
version: 2.0.0
author: Solaris
license: MIT
---

# Solaris — admin act

The operator soul's hands. After `solaris-admin-diagnose` / `solaris-admin-logs`
find the problem, this skill changes the box's state through the
**`servicebay_admin`** MCP.

The token is scoped **read + lifecycle + mutate** and nothing more — there is no
`destroy` or `exec` scope, so delete/purge/wipe/factory-reset/reboot/shell are
unreachable. If an operator asks for one, say it's outside the operator soul's
permission and stop.

## When to use

- "Starte Jellyfin neu." / "Restart Jellyfin."
- "Stopp den Media-Stack." / "Deploy Solaris neu." / "Redeploy Solaris."
- "Fix die Proxy-Route für Chat." / "Ändere die Service-Config von …"

Out of scope: figuring out *what* to act on (`solaris-admin-diagnose` /
`solaris-admin-logs` first); anything destroy/shell-shaped (unreachable — say so).

## Operating sequence

1. **Know the target.** Resolve the service/container as
   `solaris-admin-diagnose` does (`list_services` / `list_containers`). Don't act
   blind — do a quick read first if you weren't given a diagnosis.
2. **Classify:** lifecycle (start/stop/restart — reversible, run directly) vs
   mutate (redeploy, config edit, proxy-route — impactful, confirm first).
3. **Confirm impactful mutations.** State in one line what changes and the visible
   effect ("Ich deploye Solaris neu — der Agent ist ~30 s offline. Soll ich?") and
   wait for an explicit yes. A single lifecycle restart needs no prompt unless
   several were asked for at once.
4. **Run it** via the matching `servicebay_admin` tool.
5. **Verify.** Re-check state (`list_services` / `get_health_checks`) and report
   the concrete outcome. If it didn't take, read the logs rather than retrying blind.

## Tool cheat sheet

| Action | Class | servicebay_admin tool |
|---|---|---|
| Start / Stop / Restart a service | lifecycle | start / stop / restart action |
| Redeploy a service | mutate | redeploy/deploy — **confirm first** |
| Edit service config / files | mutate | update-service — **confirm first** |
| Change a proxy route | mutate | proxy-route update — **confirm first** |

Use the tool names the live MCP advertises rather than guessing.

## Failure paths

- `servicebay_admin` unreachable → "Ich erreiche ServiceBay gerade nicht — ich
  kann auf der Box nichts ändern." Don't half-apply.
- Action errors → report it in plain language, leave the service as-is, offer to
  read the logs; don't loop retries.
- A destroy/shell request → "Das kann die Operator-Seele nicht — sie hat dafür
  keine Berechtigung."

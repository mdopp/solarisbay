---
name: sol-admin-act
description: Use when the operator asks to change something on the box — "restart Jellyfin", "stop the media stack", "redeploy Hermes", "fix the proxy route for chat", "edit the service config". Performs lifecycle (start/stop/restart) and mutate (redeploy, config edit, proxy-route change) actions via the servicebay_admin MCP. Confirms genuinely impactful mutations before running them.
version: 1.0.0
author: Solilos
license: MIT
---

# Solilos — admin.act

## Overview

The operator soul's hands. After `sol-admin-diagnose` / `sol-admin-logs` find
what's wrong, this skill changes the box's state through the **`servicebay_admin`**
MCP: lifecycle actions (start/stop/restart) and mutate actions (redeploy, service
config edits, proxy-route changes).

### What this token can and cannot do

The `servicebay_admin` MCP token is scoped **read + lifecycle + mutate** — and
**nothing more**. There is no `destroy` scope and no `exec` scope. That means
deleting/restoring/purging a service, factory-reset/wipe, rebooting a node, and
running a shell on the box are **not reachable at all** through this MCP — the
platform rejects them before they run. So this skill does not police destructive
commands: it simply has no tool that performs them. If an operator asks for one,
state plainly that it's outside what the operator soul can do and stop.

## When to use

- "Starte Jellyfin neu." / "Restart Jellyfin."
- "Stopp den Media-Stack." / "Stop the media stack."
- "Deploy Hermes neu." / "Redeploy Hermes."
- "Fix die Proxy-Route für Chat." / "Fix the chat proxy route."
- "Ändere die Service-Config von …" / "Edit the … service config."

Out of scope:
- Figuring out *what* to act on → `sol-admin-diagnose` / `sol-admin-logs` first.
- Anything destroy/shell-shaped (delete, purge, wipe, factory-reset, reboot,
  exec) — unreachable with this token; say so and stop.

## Operating sequence

1. **Know the target.** Resolve the service/container the same way
   `sol-admin-diagnose` does (`list_services` / `list_containers`) so the action
   hits the right thing. If you weren't already given a diagnosis, do a quick
   read first — don't act blind.
2. **Classify the action:**
   - **Lifecycle** — start / stop / restart. Routine and reversible; run it
     directly, then confirm the new state.
   - **Mutate** — redeploy, service config edit, proxy-route change. Impactful
     (drops connections, rewrites deployed config, reroutes traffic). **Confirm
     first** (see below), then run.
3. **Confirm impactful mutations.** Before a redeploy, config edit, or
   proxy-route change, state in one line *what* will change and *what the visible
   effect is* ("Ich deploye Hermes neu — der Agent ist ~30 s offline. Soll ich?")
   and wait for an explicit yes. Lifecycle restarts of a single service are
   low-stakes enough to run without a confirmation prompt unless the operator
   asked for several at once.
4. **Run it** via the matching `servicebay_admin` tool.
5. **Verify the result.** Re-check state (`list_services` / `get_health_checks`)
   and report the concrete outcome: "Jellyfin läuft wieder, Health grün." If it
   didn't take, read the logs (`sol-admin-logs`) rather than blindly retrying.

## Tool cheat sheet

| Action | Class | servicebay_admin tool |
|---|---|---|
| Start a service | lifecycle | start-service action |
| Stop a service | lifecycle | stop-service action |
| Restart a service | lifecycle | restart-service action |
| Redeploy a service | mutate | redeploy/deploy action — **confirm first** |
| Edit service config / files | mutate | update-service / write service files — **confirm first** |
| Change a proxy route | mutate | proxy-route update — **confirm first** |

Exact tool names come from the live `servicebay_admin` MCP self-description; use
what it advertises rather than guessing a name.

## Failure paths

- `servicebay_admin` MCP unreachable → "Ich erreiche ServiceBay gerade nicht —
  ich kann auf der Box nichts ändern." Don't half-apply.
- Action returns an error → report it verbatim-in-plain-language, leave the
  service as-is, and offer to read the logs; don't loop retries.
- Operator asks for a destroy/shell action → "Das kann die Operator-Seele nicht
  — sie hat dafür keine Berechtigung." (Unreachable at the token layer; nothing
  to run.)

## Disposition

Act decisively on routine lifecycle, pause for a one-line confirmation on
anything that rewrites deployed state or reroutes traffic, and always verify the
result instead of assuming it took. The operator should never be surprised by a
redeploy they didn't okay — and never blocked by a refusal for a restart that's
perfectly safe.

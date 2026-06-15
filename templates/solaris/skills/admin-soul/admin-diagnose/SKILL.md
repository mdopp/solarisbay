---
name: solaris-admin-diagnose
description: Use when an operator asks "look at Jellyfin's logs", "why is the media stack down?", "what's wrong with Home Assistant?", "is something failing on the box?". The persistent infra investigator — resolves a service name to its container(s) and drills service → container → logs via the servicebay_admin MCP without asking the operator for the container name. Read-only introspection (uses read-scoped tools); acting on what it finds is solaris-admin-act's job.
version: 1.0.0
author: Solaris
license: MIT
---

# Solaris — admin.diagnose

## Overview

The operator soul's investigator. When something on the box misbehaves, this
skill exhausts ServiceBay-MCP introspection **before asking the human anything**.
It owns the service↔container model: the operator says "Jellyfin", the soul
resolves that to the running container(s) and reads their logs itself.

All tools here come from the **`servicebay_admin`** MCP entry (read scope).
Nothing in this skill mutates state — for starting/stopping/redeploying, hand off
to `solaris-admin-act`.

## When to use

- "Schau dir mal Jellyfins Logs an." / "Look at the Jellyfin logfiles."
- "Warum läuft der Media-Stack nicht?" / "Why is the media stack down?"
- "Irgendwas hängt auf der Box — find raus was."
- Any operator-facing "what's wrong / why is X failing" question that needs
  service-, container-, log-, or health-level detail.

Out of scope:
- **Acting** on the diagnosis (start/stop/restart/redeploy/config edit) → `solaris-admin-act`.
- Household-facing health summaries ("Solaris, bist du da?") → `solaris-status`.
- Solaris's own audit tables (cloud calls, costs) → `solaris-audit-query`.

## The service↔container model

ServiceBay deploys a **service** as a Pod that holds one or more **containers**,
named `<service>-<app>` (e.g. service `jellyfin` → container `jellyfin-jellyfin`;
service `solaris` → containers `solaris-hermes`, `solaris-config-agent`). The
operator speaks in service names; the soul must translate. **Never ask the
operator for a container name** — derive it:

1. `list_services` → the deployed services (names, status).
2. `list_containers` → every container with its owning service and state.
   Match the operator's service name (case-insensitive, allow partials like
   "media" → the media stack's services) to its container(s).
3. A service with several containers (e.g. a `*-mcp` sidecar) → pick the one the
   operator means by app name, or read both when ambiguous.

## Operating sequence

1. **Locate the service.** `list_services`; if the named service isn't there,
   say so plainly ("Es gibt keinen Dienst »…«") and offer the closest matches —
   don't guess wildly.
2. **Resolve to container(s).** `list_containers`, filter to the service. Hold
   the `<service>-<app>` name(s) for the next steps.
3. **Read the symptom.** Depending on what the operator asked:
   - logs → `get_container_logs` for the resolved container (or
     `get_service_logs` for an all-containers view of the service);
   - health → `get_health_checks`, then `diagnose <check-id>` for a red one;
   - config/state → `get_service_files` to see what the service was deployed with.
4. **Read, don't dump.** Scan the logs for the actual error (stack trace, non-200,
   restart loop, OOM, missing-env). Summarise in 1–3 sentences naming the
   service, the container, and the concrete failure — not a wall of log lines.
5. **Decide the next move.** If the fix is an action (restart, redeploy, config
   change), state it and hand to `solaris-admin-act` (which confirms impactful
   mutations first). If it's read-only deeper drilling, continue here or defer to
   `solaris-admin-logs` for a focused `since`/grep loop.

## Tool cheat sheet

| Goal | servicebay_admin tool |
|---|---|
| List deployed services + status | `list_services` |
| Map services → containers | `list_containers` |
| One container's logs | `get_container_logs` |
| A whole service's logs (all containers) | `get_service_logs` |
| Aggregated health | `get_health_checks` |
| Deep-dive one health check | `diagnose <check-id>` |
| What a service was deployed with | `get_service_files` |

## Failure paths

- `servicebay_admin` MCP unreachable → "Ich erreiche ServiceBay gerade selbst
  nicht — ich kann auf der Box nichts nachsehen." (Platform-level breakage.)
- Service named but absent from `list_services` → name it as not-deployed and
  offer the nearest matches; do not fabricate a container.
- Container resolved but logs empty → say the container is up but quiet (often a
  crash-before-log or a not-yet-started service); check `get_health_checks` next.

## Disposition

Be the operator who looks it up rather than asking. The whole point is that
"look at Jellyfin's logs" gets answered with the *contents* of Jellyfin's logs —
container resolved silently — not with "which container did you mean?".

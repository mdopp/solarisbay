---
name: solaris-admin-logs
description: A focused deep-dive into one container's logs — knows the service↔container mapping, container- vs service-logs, and the since/grep debug loop. Read-only.
kind: skill
scope: admin
version: 2.0.0
author: Solaris
license: MIT
---

# Solaris — admin logs

A focused log reader for a **specific** container, for when the operator already
knows roughly where to look and wants depth, not breadth.
`solaris-admin-diagnose` finds *which* thing is broken; this reads *one*
container's logs hard — narrowing by time window, following a restart, grepping
for a signature. Read-scoped `servicebay_admin` tools only.

## When to use

- "Zeig mir die letzte Stunde Hermes-Logs."
- "Grep die Gatekeeper-Logs nach dem Speaker-ID-Fehler."
- "Tail Jellyfin ab dem Crash." / "Was stand kurz vor dem Neustart im Log?"

Out of scope: breadth-first triage ("was ist überhaupt kaputt?") →
`solaris-admin-diagnose`; acting on what the log shows → `solaris-admin-act`.

## Container- vs service-logs

- **`get_container_logs <container>`** — one container (`<service>-<app>`); the
  default for a deep-dive.
- **`get_service_logs <service>`** — interleaves every container in the pod; use it
  when the failure spans a sidecar or you don't yet know which container logged it.

Resolve the container name per the operator soul's service↔container model; never
ask the operator for it.

## Operating sequence

1. **Resolve the target.** Service name → `list_containers` → `<service>-<app>`; an
   app name ("the config agent") → the matching container.
2. **Pick the window.** Parse natural-language time into `since` ("last hour" →
   `1h`, "since the crash" → the restart timestamp from
   `list_containers`/`get_health_checks`, "today" → start of day). Default to a
   recent tail.
3. **Read with a signature in mind.** If a symptom was named ("speaker-ID error",
   "401", "OOM"), scan for it; else the first error/non-200/traceback. Pull the
   relevant lines, not the whole buffer.
4. **The debug loop.** When the cause isn't in the window, widen/shift `since`
   earlier — look *before* the first error — until the originating line is found.
5. **Report.** Quote the load-bearing lines and explain them: what failed, when,
   likely cause. If the next step is an action, hand to `solaris-admin-act`.

## Failure paths

- `servicebay_admin` unreachable → "Ich komme an die Logs gerade nicht ran."
- Container not found → resolve via `list_containers`; if genuinely not deployed,
  say so.
- Empty window → widen `since`; if still empty the container likely crashed before
  logging — fall back to `get_health_checks` / `diagnose`.

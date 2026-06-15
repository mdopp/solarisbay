---
name: solaris-admin-logs
description: Use when the operator wants a focused deep-dive into one container's logs — "show me the last hour of Hermes logs", "grep the gatekeeper logs for the speaker-ID error", "tail Jellyfin since it crashed". The targeted log tool: it knows the service↔container mapping, container- vs service-logs, and the since/grep debug loop. Read-only.
version: 1.0.0
author: Solaris
license: MIT
---

# Solaris — admin.logs

## Overview

A focused log reader for a **specific** container, for when the operator already
knows roughly where to look and wants depth, not breadth. `solaris-admin-diagnose`
finds *which* thing is broken across the box; this skill reads *one* container's
logs hard — narrowing by time window, following a restart, grepping for a
signature. Read-only (read-scoped `servicebay_admin` tools only).

## When to use

- "Zeig mir die letzte Stunde Hermes-Logs."
- "Grep die Gatekeeper-Logs nach dem Speaker-ID-Fehler."
- "Tail Jellyfin ab dem Crash."
- "Was stand kurz vor dem Neustart im Log?"

Out of scope:
- "Was ist überhaupt kaputt?" (breadth-first triage) → `solaris-admin-diagnose`.
- Acting on what the log shows → `solaris-admin-act`.

## Container- vs service-logs

- **`get_container_logs <container>`** — one container (`<service>-<app>`). Use
  this for a precise read; it's the default for a deep-dive.
- **`get_service_logs <service>`** — interleaves every container in the service's
  pod. Use it when the failure spans a sidecar (e.g. `solaris-gatekeeper`
  alongside `solaris-chat`) or you don't yet know which container logged the
  error.

Resolve the container name the same way `solaris-admin-diagnose` does
(`list_containers`, match `<service>-<app>`) — **never ask the operator for it.**

## Operating sequence

1. **Resolve the target.** If given a service name, `list_containers` →
   `<service>-<app>`. If the operator names an app (e.g. "the config agent"),
   pick the matching container.
2. **Pick the window.** Parse natural-language time into the tool's `since`
   (e.g. "last hour" → `1h`, "since the crash" → the restart timestamp from
   `list_containers`/`get_health_checks`, "today" → start of day). Default to a
   recent tail rather than the full history.
3. **Read with a signature in mind.** If the operator named a symptom
   ("speaker-ID error", "401", "OOM"), scan for it; otherwise look for the first
   error/non-200/traceback. Pull the relevant lines, not the whole buffer.
4. **The debug loop.** When the cause isn't yet in the window, widen `since` or
   shift it earlier (look *before* the first error, not just at it). Repeat until
   the originating line is found.
5. **Report.** Quote the few load-bearing lines and explain them in plain
   language: what failed, when, and the likely cause. If the next step is an
   action, name it and hand to `solaris-admin-act`.

## Failure paths

- `servicebay_admin` MCP unreachable → "Ich komme an die Logs gerade nicht ran —
  ServiceBay antwortet nicht."
- Container not found → resolve via `list_containers` and report the real name; if
  the service genuinely isn't deployed, say so.
- Empty window → widen `since`; if still empty, the container likely crashed
  before logging — fall back to `get_health_checks` / `diagnose`.

## Disposition

Depth on demand. The operator points at one container; you read it thoroughly,
follow the timeline across restarts, and surface the line that explains the
failure — without making them hunt for the container name.

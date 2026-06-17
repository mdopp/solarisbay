---
name: solaris-debug-set
description: Turn cluster-wide debug-mode on/off (optionally for a bounded window). Writes system_settings.debug_mode in solaris.db; components pick it up within ~5s. Admin-only.
kind: command
scope: admin
command: /debug
version: 1.0.0
author: Solaris
license: MIT
---

# Solaris — debug set

**Usage:** `/debug on [<dauer>]` · `/debug off` · `/debug status`

Cluster-wide debug-mode toggle. When on, Solaris containers log full prompts /
responses / tool args / connector bodies, retention is suspended, and
cloud-LLM-fulltext fields are returned by `solaris-audit-query` instead of
redacted. Source of truth is the `debug_mode` row in `system_settings` in
`solaris.db` (default `/var/lib/solaris/solaris.db`); components re-query on every
audit event (no caching > 5 s), so the change propagates within ~5 seconds with no
restart.

## Hard guards

- **Admin gate.** Confirm the active harness includes the `admins` group before any
  write; else refuse: "Only an admin can change debug mode."
- **Always show what was set.** After a write, read the row back and confirm:
  "Debug-Mode an bis 14:30 Uhr." / "Debug-Mode aus."
- **Default to a TTL.** On "on" without a duration, suggest a bounded window
  ("Eine Stunde okay?") rather than leaving it on indefinitely.

## Set

Update the `system_settings` row keyed `debug_mode` with a JSON value:

```json
{"active": true, "verbose_until": "2026-05-16T15:30:00+00:00", "latency_annotations": false}
```

- `active`: bool — global on/off.
- `verbose_until`: ISO-8601 or `null` — TTL after which `effective = false`.
- `latency_annotations`: bool — adds "STT 230ms · router 80ms → 12B local · 1.4s"
  markers on voice responses (admin uids only; hide from family members).

## Status

`SELECT value, updated_at FROM system_settings WHERE key='debug_mode'`, then:

```
effective_active = value.active AND (value.verbose_until IS NULL OR now() < value.verbose_until)
```

## Privacy reminder

Turning debug-mode on starts logging full conversation content (prompts +
responses) to `cloud_audit` and stdout. Mention this the first time in a session
("Debug-Mode loggt jetzt auch Volltexte.").

## Failure paths

- `solaris.db` unreachable → "Ich kann debug-mode gerade nicht ändern." Don't loop.
- `verbose_until` in the past → reject as nonsense, ask back.

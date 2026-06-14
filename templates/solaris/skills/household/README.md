# Solaris skills

Household-specific skills consumed by [Hermes Agent](https://github.com/NousResearch/hermes-agent).

Hermes provides the agent loop, skill registry, cron, messaging gateways, and the self-improvement loop natively. Solaris contributes only the **household-specific** procedures tied to *our* SQLite schema (`solaris.db`) or *our* policy choices (cloud audit).

The `solarisbay` ServiceBay template bind-mounts this directory into the Hermes container at `/opt/data/skills/solaris`, alongside the path to `solaris.db`. Hermes loads everything here on startup.

## Currently registered skills

| Directory | `name:` | Phase | One-liner |
|---|---|---|---|
| `status/` | `solaris-status` | 0 | Pings every Solaris dependency (`solaris.db`, Hermes, Ollama, Home Assistant, ServiceBay-MCP; voice probes once Phase 1 voice is deployed) and returns per-component status. Read-only. |
| `audit-query/` | `solaris-audit-query` | 0 | Read-only query over `cloud_audit` (and future Phase-3a household-domain tables) in `solaris.db`. |
| `debug-set/` | `solaris-debug-set` | 0 | Admin: toggle `system_settings.debug_mode` row in `solaris.db` (verbose logging on demand, TTL-bounded). |
| `problem-summarizer/` | `solaris-problem-summarizer` | 0 | Distils resolved problem→indicators→solution sequences from system logs + past diagnostic chats into a structured Markdown KB at `/opt/data/notes/knowledge-base/troubleshooting.md`. On-request + weekly cron. |

All three operate directly against `solaris.db` (inline SQLite) and ServiceBay-MCP (`get_health_checks`/`diagnose`) — no external `solaris_*` libraries or separate companion scripts.

## What's *not* a skill in Solaris

| Capability | Lives in |
|---|---|
| Lights / heating / scenes | Hermes Skills Hub — `smart-home/home-assistant` skill (PR'd from Solaris's removed `light/`) |
| Help (`/skills`, `/help`) | Hermes native |
| Timers / alarms / reminders / recurring tasks | Hermes cron |
| Skill management, authorship, review, revert | Hermes' built-in skill management + self-improvement loop |
| Messaging-gateway pairing, identity-link | Hermes' messaging-gateway pairing |

Context: [`../solaris-architecture.md`](../solaris-architecture.md).

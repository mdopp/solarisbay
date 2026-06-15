---
name: solaris-status
description: Use when the user asks "is Solaris alive?", "is everything working?", "why isn't the light responding?", or any other "health-check" style question. Probes the configured Solaris dependencies (solaris.db, Ollama, Home Assistant, ServiceBay-MCP) and reports per-component status. Read-only.
version: 0.3.0
author: Solaris
license: MIT
---

# Solaris — status

## Overview

Quick "is everything OK?" probe across every Solaris dependency. Read-only — no state changes.

## When to use

- "Solaris, bist du da?" / "Bist du wach?"
- "Funktioniert alles?" / "Geht das Licht gerade nicht?"
- "Ist Home Assistant erreichbar?"
- "Wo hakt's gerade?"
- As the **first** diagnostic step before deeper drill-down — if `solaris-status` says everything's green, the bug is application-side, not infrastructure.

## Operating sequence

1. Call ServiceBay-MCP `get_health_checks` to retrieve the platform's aggregated health state.
2. For each result, the platform returns the canonical shape:
   ```json
   {
     "ok": false,
     "results": [
       {"name": "ollama", "ok": true, "latency_ms": 8, "type": "http"},
       {"name": "home-assistant", "ok": false, "latency_ms": 3000, "type": "http", "detail": "ConnectError: ..."},
       {"name": "solaris.db", "ok": true, "latency_ms": 1, "type": "script"}
     ]
   }
   ```
3. If a result needs deeper context (specific error chain, last successful run, history), call `diagnose <check-id>` for that one check.
4. Summarise verbally:
   - **All green** → "Alles ok." or "Alles grün."
   - **One red** → name it: "Home Assistant antwortet nicht — ich erreiche die Haussteuerung gerade nicht."
   - **Multiple red** → group by impact: "Ollama und Home Assistant sind beide down — das ist ernst."

## What gets probed

This skill **does not** define what gets probed. The set of health checks is **declared at deploy time** by each template's `post-deploy.py` via `create_health_check` against ServiceBay-MCP. `solarisbay` registers:

| Check | Type | Purpose |
|---|---|---|
| `solaris.db` | `script` | SQLite open + `SELECT 1` on `cloud_audit` — Solaris's audit state readable |
| `ollama` | `http` | Local LLM responding to `/api/tags` |
| `home-assistant` | `http` | Home Assistant reachable (reached via the engine's HA tools/MCP) |
| `servicebay-mcp` | `http` | Platform control surface reachable |
| `gatekeeper` *(Phase 1)* | `http` | Gatekeeper container's internal `/push/health` |
| `voice-whisper` *(Phase 1)* | `podman` | Whisper container running |
| `voice-piper` *(Phase 1)* | `podman` | Piper container running |

ServiceBay's existing templates (`home-assistant`, `media`, …) register their own checks the same way. The full check set lives in ServiceBay's HealthStore.

## What this does NOT cover

- **Skill correctness** — we know Hermes is reachable, not that a specific skill behaves. For that, `solaris-audit-query` over `cloud_audit` and the relevant SKILL events.
- **Voice latency** — `podman`/`http` checks say the service is up, not that it's fast. For latency hunting, `solaris-debug-set` + the gatekeeper's `gatekeeper.transcript` / `gatekeeper.response` timestamps.
- **HA device state** — "is the office light actually on?" is an HA-tool query, not a status probe.

## Failure paths

- ServiceBay-MCP unreachable → respond "Ich kann das gerade selbst nicht prüfen — ServiceBay antwortet nicht." Points at something fundamentally broken at the platform level (network, auth, ServiceBay itself).

## Phase mapping

| Phase | Checks registered by solarisbay's post-deploy |
|---|---|
| **0 (now)** | solaris.db, ollama, home-assistant, servicebay-mcp |
| **1** | + gatekeeper, voice-whisper, voice-piper |
| **2** | + gatekeeper-speaker-id (model loaded? embeddings table populated?) |
| **3a** | + ingestion-pipeline backlog (rows in incoming state) |

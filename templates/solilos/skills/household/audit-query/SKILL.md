---
name: sol-audit-query
description: Use when the user asks "what happened today?", "show me errors in the last hour", "what did the cloud connector cost yesterday?". Reads from Solilos's SQLite tables (cloud_audit, system_settings) in solilos.db. Read-only — never mutates state.
version: 0.3.0
author: Solilos
license: MIT
---

# Solilos — audit.query

## Overview

Generic filter over Solilos's domain-audit tables in `solilos.db`. One query returns a JSON page of rows; the agent summarises in natural language for the user.

Currently one stream:
- `cloud_audit` — every cloud-LLM call (timestamp, uid, trace_id, vendor, lengths, latency, cost-estimate, router score + reason; prompt/response fulltext only when debug-mode is on)

More streams plug into the same dispatch as Phase 3+ tables land (book/record/document collections, ingestion_classifications, etc.).

## When to use

- "What did Solilos send to the cloud today?"
- "Wieviel hat das gestern gekostet?"
- "Show me errors in the last hour."
- "Find every event tied to trace_id <X>."

Out of scope:
- Anything that mutates state (use `sol-debug-set` for the debug-mode flag).
- Reading **operational** logs (stdout-JSON). Those go through ServiceBay-MCP `get_container_logs` — different mechanism entirely.
- Conversation history / messaging gateway state — Hermes owns those (SQLite + its own admin commands).

## Operating sequence

1. Parse the user request into:
   - `stream` (which table): currently always `cloud_audit`.
   - `since` / `until`: parse natural-language time. "today" → `today`. "last hour" → `1h`. "yesterday evening" → ISO timestamp.
   - filter fields (`uid`, `vendor`, `trace_id`, `min_cost_micro_usd`) as they apply.
2. Open `solilos.db` (path from `SOLILOS_DB_PATH`, default `/var/lib/solilos/solilos.db`) and run a parameterised SELECT against `cloud_audit` with the filters above. Apply `LIMIT` (default 50, max 200).
3. Shape the result as:
   ```
   {"ok": true, "stream": "cloud_audit", "count": 7, "rows": [...]}
   ```
4. Summarise verbally in 1–3 sentences. **Don't read UUIDs or hashes aloud.** Aggregate when sensible: "Heute 7 Cloud-Anfragen, alle Claude Sonnet, Gesamt ~12 Cent, längste 2.3 s."

## Filter cheat sheet

| Stream | Useful filters |
|---|---|
| `cloud_audit` | `--since`, `--uid`, `--vendor`, `--trace-id`, `--min-cost-micro-usd` |

`--limit` defaults to 50; bump to 200 for trends but don't read all rows back, summarise.

## Failure paths

- `solilos.db` missing or unreadable → brief: "Ich kann das Audit-Log gerade nicht lesen."
- Unknown stream → "Den Audit-Stream gibt's nicht." (Should never happen with proper parsing.)
- Empty result → "Heute hat Solilos nichts an die Cloud geschickt."

## PII

`cloud_audit.prompt_fulltext` / `response_fulltext` are returned only when `system_settings.debug_mode.active = true` (read live from `solilos.db`). Otherwise the rows return the metadata (lengths, hash, latency, cost) and the fulltext columns are nulled out — make the masking explicit in the summary if it matters. **Don't try to reconstruct prompts from hashes.**

For deep debugging of a specific failure: instruct the user to flip debug-mode for a short window via `sol-debug-set`, re-run the failing query, then turn debug-mode off.

## Phase mapping

| Phase | Streams |
|---|---|
| **0 (now)** | cloud_audit |
| **2** | + gatekeeper_decisions (speaker-ID confidence, harness chosen, embedding distance) |
| **3a** | + ingestion_classifications (Gemma vision class, confidence, final domain) |

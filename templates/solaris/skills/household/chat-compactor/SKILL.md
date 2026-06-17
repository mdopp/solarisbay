---
name: solaris-chat-compactor
description: The overnight context-compaction scheduler — sweeps stale, long chats and compacts each one.
kind: scheduler
scope: system
schedule: 15 4 * * *
version: 2.0.0
author: Solaris
license: MIT
---

# Solaris — Chat Compactor (overnight context compaction)

**Fires:** `15 4 * * *` (daily 04:15, unattended)

The nightly sweep that compacts stale, long chat sessions. This runs as a
backend **code job** (not an agent prompt): the engine picks sessions inactive
for ~a week that carry enough transcript to free something, then for each one
extracts durable learnings into memory **first** and summarizes the transcript
**second** — the order that never loses data. The original transcript is never
deleted; the summary seeds a fresh continuation. The durable household session
is skipped in place (never forked).

The live per-turn hard-cap compaction (a turn finding a chat near the context
limit) is automatic in the chat backend and is **not** this job.

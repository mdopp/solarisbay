---
name: sol-chat-compactor
description: Use as the unattended overnight cron that compacts stale, long chat sessions — first extracting durable learnings into long-term memory, then summarizing the transcript so the conversation can continue in a small context. Also usable on request when an admin asks to "compact old chats", "free up context", or "tidy the long conversations". It never runs the live per-turn hard-cap path (that is automatic in the chat backend); this skill is the scheduled overnight sweep.
version: 1.0.0
author: Solilos
license: MIT
---

# Solilos — Chat Compactor (overnight context compaction)

## Overview

Long chats grow past the model's context window and bury durable knowledge in
transcript. This skill is the **overnight half** of compaction (#210): it sweeps
stale, long conversations and compacts each one in the order that never loses
data — **extract durable learnings into memory first, summarize the transcript
second.**

The **per-turn hard-cap** trigger (compacting a chat the moment a turn finds it
near ~90-95% of the context window) is handled automatically by the chat backend
and is **not** this skill's job. This skill is the scheduled overnight sweep for
chats that went stale without crossing the cap mid-conversation.

**Two ways this runs:**
- **Unattended cron** — `solbay`'s post-deploy registers a Hermes job
  (`15 4 * * *`, daily 04:15) that fires this skill with no one present. In that
  mode you must **not** ask anyone for input.
- **On request** — an admin asks to compact old chats / free up context.

## When to use

- The nightly cron job fires this skill (unattended).
- An admin asks to compact stale chats:
  - "Compact the old chats."
  - "Fass die alten, langen Unterhaltungen zusammen und gib Kontext frei."

Out of scope:
- The live per-turn hard-cap compaction — automatic in the chat backend; do not
  duplicate it here.
- Deleting chats — compaction never destroys the original transcript.

## Operating sequence

### 1. Find stale, long conversations
Use your session tools to list conversations and pick the candidates worth
compacting: sessions that have been **inactive** for a while (e.g. last activity
more than a day ago) AND are **long** (high message/token count). Skip short or
recently-active chats — there is nothing to free. Skip maintenance sessions.

### 2. For each candidate, extract durable learnings FIRST
Before summarizing or compacting anything, pull the durable, reusable knowledge
out of the conversation and **save it to your memory** with the `fact_store`
tool — facts, decisions, household preferences, device/room/entity mappings,
people, recurring routines. One short standalone fact per learning. This is the
step that guarantees nothing durable is lost when the transcript is later
compacted. Store nothing that is transient small-talk or already obvious; a chat
with nothing durable simply gets no new facts (no fabrication).

### 3. Then summarize the transcript
Only after the learnings are stored, write a compact summary of the conversation
— the topic, what was decided/done, any open thread, the user's last intent —
terse and factual. This summary is what lets the chat continue in a small context.

### 4. Never destroy the original
Compaction is **continuation, not deletion**: the original session/transcript
stays as the durable record. Do not delete a chat. The summary seeds a fresh
continuation; the learnings live in memory and are recalled on demand.

### 5. Confirm (interactive runs only)
- Example: *"Ich habe N alte Unterhaltungen verdichtet — Lernpunkte gespeichert,
  Verläufe zusammengefasst."*
- *Unattended cron run* — there is no one to tell; just do the work. If nothing
  was stale enough to compact, do nothing rather than inventing work.

## Guards

- **Order is mandatory**: extract learnings to memory **before** summarizing.
  Never summarize-then-extract — a learning lost before it is stored is gone.
- **No fabrication**: a quiet night with no stale long chats compacts nothing;
  store no invented facts.
- **No deletion**: this skill records and summarizes; it never deletes a chat or
  any other data.
- **Don't self-schedule**: the cron is registered once by `solbay`'s post-deploy.
  This skill never creates or edits its own cron job.
- **Privacy**: store the durable household fact, not unrelated chat content or
  who said it, and honor per-resident scope.

## Failure paths

- Session tools unavailable → skip this run rather than guessing; log nothing
  durable lost (originals are untouched).
- Memory (`fact_store`) unavailable for a chat → **do not** summarize/compact
  that chat this run (extracting first is the whole safety property); move on.

## Verification checklist

1. Confirm the daily cron is registered: `GET /api/jobs` (or `hermes cron list`)
   shows `sol-chat-compactor` at `15 4 * * *`.
2. Seed a long, stale chat, force a run, and confirm: durable facts from it are
   retrievable from memory afterward, and the original transcript still exists
   (nothing deleted).
3. Confirm a short/recent chat is left untouched.

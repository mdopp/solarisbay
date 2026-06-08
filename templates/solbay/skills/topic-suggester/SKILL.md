---
name: sol-topic-suggester
description: Use when Solilos notices the current conversation keeps circling a single nameable theme (a project, place, recurring household concern) that has come up across several recent sessions but has no topic assigned yet — e.g. the resident keeps talking about the Wintergarten-Umbau, the new boiler, a trip they are planning. Solilos then offers "Soll ich dafür ein Topic '<Name>' anlegen?"; only on the resident's yes does it create the topic and assign the current chat to it. Suggestion-only — never auto-creates, never re-prompts after a no.
version: 1.0.0
author: Solilos
license: MIT
---

# Solilos — Topic Suggester (propose + create-on-confirm)

## Overview

The **suggested half** of topic creation (architecture D4, #245). Topics
otherwise only grow when a resident creates one manually via the chat-header
picker. This skill lets Solilos *notice* that a conversation is about a coherent,
recurring theme that has no topic yet and **offer** to create one — but creation
only happens after the resident confirms. The manual half (the picker, #241)
stays the always-available alternative.

Detection in this v1 is **agent-judgment-driven**, not a separate classifier:
*you* decide a theme is recurring from what you can see (this conversation plus
the recent notes/holographic context). There is no embedding model and no
background job — the trigger is your own read of the conversation, gated by the
two registry checks below so you never suggest something that already exists or
nag a resident who said no.

## When to use

- This chat clearly centres on **one nameable theme** — a project
  ("Wintergarten-Umbau"), a place ("das Gartenhaus"), a recurring concern
  ("Heizung", "die Reise nach Schweden") — and
- that theme has **come up before** across recent sessions (you have seen it in
  the notes / earlier context), and
- the current chat has **no primary topic** assigned, and
- **no existing topic already covers it** (registry check below).

When all four hold, offer once. Natural triggers: mid-conversation once the
theme is obvious, or at session end (e.g. when the `sol-chat-compactor` overnight
sweep summarises a chat and sees the same untagged theme recurring).

Out of scope:
- The resident explicitly asking to create a topic → that is the picker / a
  direct create, not a suggestion. Just create it (steps 4–5) without the offer.
- Assigning or switching topics on a chat that already has one → the picker
  (#241), not this skill.
- A one-off mention, or a theme that already maps to an existing topic → do
  **not** suggest (it would be noise).

## Operating sequence

### 1. Read the active context

- The active resident's uid (`<uid>`) and the current chat's session id
  (`<session_id>`) come from the turn context. If the turn already carries an
  `[Active topic: … #topic/<slug>]` line, the chat **already has a topic** —
  stop, do not suggest.

### 2. Check the registry — does a topic already cover this theme?

Read the topics registry directly (same `solilos.db` the `audit-query` skill
reads; path from `SOLILOS_DB_PATH`, default `/var/lib/solilos/solilos.db`).
List the resident's visible topics:

```bash
sqlite3 "${SOLILOS_DB_PATH:-/var/lib/solilos/solilos.db}" \
  "SELECT slug, display_name FROM topics
    WHERE archived = 0
      AND (owner_uid = '<uid>' OR owner_uid IS NULL OR scope != 'resident');"
```

If the theme matches an existing topic (by display name or slug), **do not
suggest** — either it is already in use elsewhere, or it is the chat's topic and
step 1 already stopped you. Only proceed when no existing topic fits.

### 3. Offer — and stop if the resident declines

Propose in German, naming the theme you detected:

> "Mir fällt auf, dass es öfter um **<Thema>** geht. Soll ich dafür ein eigenes
> Topic „<Vorschlag>" anlegen?"

- Offer the theme as an editable name — the resident may rename it or pick a
  different slug. Slugify the agreed name: lower-case, spaces → `-`, a hierarchy
  joined by `/` (e.g. "Projekt Wintergarten" → `projekt/wintergarten`).
- **On a no / "lass mal" / silence**: do nothing. **Do not re-prompt** for this
  theme in this conversation — one offer, then drop it.
- Only on an explicit **yes** do you continue to step 4.

### 4. Create the topic (only after yes)

POST to the chat proxy's topics endpoint — the proxy owns the registry write and
the per-resident scoping (it creates a `resident`-scoped row owned by `<uid>`).
The proxy listens on `127.0.0.1:8787` inside the pod (hostNetwork); pass the
resident's identity in the `Remote-User` header so the row is owned correctly:

```bash
curl -fsS -X POST http://127.0.0.1:8787/api/topics \
  -H 'Content-Type: application/json' \
  -H 'Remote-User: <uid>' \
  -d '{"slug": "projekt/wintergarten", "display_name": "Wintergarten", "color": "#22aa55"}'
```

- `color` is optional (a hex accent for the UI chip); omit it to leave it null.
- The create is idempotent on slug — if the resident already has that slug it is
  left untouched, so a re-confirmed offer never clobbers an existing topic.

### 5. Assign the new topic as the chat's primary

Make the current chat carry the topic so this and future ingestion in it gets
tagged `#topic/<slug>` (the data→topic convention, #243):

```bash
curl -fsS -X POST http://127.0.0.1:8787/api/sessions/<session_id>/topics \
  -H 'Content-Type: application/json' \
  -H 'Remote-User: <uid>' \
  -d '{"action": "primary", "slug": "projekt/wintergarten"}'
```

### 6. Confirm to the resident

Briefly, e.g.: *"Erledigt — ich hab das Topic „Wintergarten" angelegt und diese
Unterhaltung dazu sortiert. Du kannst den Namen jederzeit im Topic-Picker
ändern."*

## Guards

- **Never auto-create.** The offer is a question; the create happens only after
  an explicit yes (D4). No yes → no row, no assignment.
- **One offer per theme.** After a no, do not bring the same theme up again in
  this conversation. Respect the resident's choice.
- **Don't suggest what exists.** If a topic already covers the theme (step 2) or
  the chat already has a primary topic (step 1), stay silent.
- **Resident's own scope.** Created topics are `resident`-scoped and owned by the
  confirming resident — you never create shared/admin topics from a suggestion.
- **Editable name.** Treat your proposed name as a default the resident can
  change before confirming; use the name they settle on.

## Failure paths

- `solilos.db` missing/unreadable → the registry hasn't migrated yet; skip the
  suggestion silently rather than erroring (the picker will work once it lands).
- The create/assign `curl` fails (proxy unreachable, non-2xx) → tell the resident
  plainly that creating the topic didn't work right now and offer the manual
  picker; do not pretend it succeeded.

## Related

- Manual half: the chat-header **topic picker** (#241) — always available.
- Registry + assignment: the `topics` / `session_topics` tables and the
  `/api/topics` + `/api/sessions/<id>/topics` endpoints (#240, #241).
- Tagging downstream: `sol-dynamic-skills` / `media-ingestion-multimodal` stamp
  `#topic/<slug>` on data ingested while the topic is active (#243);
  `sol-notes-search` retrieves by that tag (#244).
- Natural session-end trigger: `sol-chat-compactor`'s overnight sweep.

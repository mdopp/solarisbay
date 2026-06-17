---
name: solaris-topic-suggester
description: When a chat keeps circling one untagged recurring theme, offer to create a topic for it and assign it on the resident's yes.
kind: hook
scope: household
event: topic-circling
version: 2.0.0
author: Solaris
license: MIT
---

# Solaris — Topic Suggester (propose + create-on-confirm)

**Binds:** `topic-circling` (the conversation keeps returning to one nameable
recurring theme that has no topic assigned yet)

The suggested half of topic creation (#245). Offer to create a topic — creation
happens **only** after the resident confirms. Suggestion-only: never auto-create,
never re-prompt after a no. Detection is your own read of the conversation plus
recent context (no classifier).

## What to do on the event

### 1. Confirm the four conditions hold
Offer only when the chat centres on **one nameable theme** (a project/place/
recurring concern), that theme has **come up before** in recent sessions, the chat
has **no primary topic** (no `[Active topic: … #topic/<slug>]` line), and **no
existing topic already covers it**. A one-off mention or an existing match → stay
silent.

### 2. Check the registry
Read the topics registry (`solaris.db`, path from `SOLARIS_DB_PATH`, default
`/var/lib/solaris/solaris.db`):

```bash
sqlite3 "${SOLARIS_DB_PATH:-/var/lib/solaris/solaris.db}" \
  "SELECT slug, display_name FROM topics
    WHERE archived = 0
      AND (owner_uid = '<uid>' OR owner_uid IS NULL OR scope != 'resident');"
```

If the theme matches an existing topic, do not suggest.

### 3. Offer — stop on a no
Propose in German, naming the theme:

> "Mir fällt auf, dass es öfter um **<Thema>** geht. Soll ich dafür ein eigenes
> Topic „<Vorschlag>" anlegen?"

Treat the name as editable. On a no / "lass mal" / silence: do nothing and **do
not re-prompt** for this theme in this conversation. Only an explicit **yes**
continues. (A resident who explicitly asks to create a topic → just create it,
skip the offer.)

### 4. Create the topic (only after yes)
Slugify the agreed name (lower-case, spaces → `-`, hierarchy joined by `/`, e.g.
"Projekt Wintergarten" → `projekt/wintergarten`) and POST to the chat proxy
(`127.0.0.1:8787`), passing the resident in `Remote-User` so the row is owned and
`resident`-scoped:

```bash
curl -fsS -X POST http://127.0.0.1:8787/api/topics \
  -H 'Content-Type: application/json' \
  -H 'Remote-User: <uid>' \
  -d '{"slug": "projekt/wintergarten", "display_name": "Wintergarten", "color": "#22aa55"}'
```

`color` is optional; the create is idempotent on slug.

### 5. Assign it as the chat's primary topic

```bash
curl -fsS -X POST http://127.0.0.1:8787/api/sessions/<session_id>/topics \
  -H 'Content-Type: application/json' \
  -H 'Remote-User: <uid>' \
  -d '{"action": "primary", "slug": "projekt/wintergarten"}'
```

### 6. Confirm
*"Erledigt — ich hab das Topic „Wintergarten" angelegt und diese Unterhaltung
dazu sortiert. Du kannst den Namen jederzeit im Topic-Picker ändern."*

## Guards

- **Never auto-create**: the create happens only after an explicit yes.
- **One offer per theme**: after a no, drop it for this conversation.
- **Don't suggest what exists** or what the chat already has.
- **Resident's own scope**: created topics are `resident`-scoped, owned by the
  confirming resident.

## Failure paths

- `solaris.db` missing/unreadable → skip the suggestion silently.
- The create/assign `curl` fails → tell the resident plainly and offer the manual
  picker; do not pretend it succeeded.

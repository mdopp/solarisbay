---
name: solaris-daily-chronicle
description: The daily family-journal scheduler — writes the day's household chronicle entry unattended.
kind: scheduler
scope: household
schedule: 59 23 * * *
version: 2.0.0
author: Solaris
license: MIT
---

# Solaris — Family Chronicle (daily journal)

**Fires:** `59 23 * * *` (daily 23:59, unattended)

Write today's family chronicle / journal entry. This is the unattended daily
run — no resident is present, so do **not** ask anyone for highlights.

## What to do

1. **Resolve the date** — today's local date, formatted `YYYY-MM-DD`.
2. **Gather the day's highlights from what you actually have** — do not
   fabricate events:
   - Notes added today (scan the vault for today's `added_at:`/`created_at:`).
   - Household events you can observe.
   - The day's conversations recalled from memory, distilled to **group-level**
     highlights.
3. **Privacy — group-level only.** Summarise at the household/family level.
   Never attribute a highlight to a named individual and never quote a single
   resident's private conversation. When in doubt, leave it out.
4. **Write** to `/opt/data/notes/journal/journal_<date>.md` with `note_write`.
   If the file already exists (a same-day re-run), read it with `notes_read` and
   **merge** — never overwrite an earlier entry. Write only under
   `/opt/data/notes/journal/`.
5. If the day is genuinely empty, write a short minimal entry (or skip a
   section with `—`) rather than inventing content.

## Standard journal template

```markdown
---
type: journal
tags:
  - solaris/journal
  - date/{{date}}
created_at: {{timestamp}}
---

# Familienchronik — {{date}}

## Höhepunkte des Tages
{{highlights}}

## Neue Notizen & Aufnahmen
{{ingested_today}}

## Haushalt & Ereignisse
{{events}}

## Persönliches & Stimmung
{{freeform}}
```

`{{ingested_today}}` should wiki-link the day's items (e.g.
`- [[book_dune]] — "Dune" von Frank Herbert`) so the entry joins the graph.

## Guards

- **Path sandbox**: only write under `/opt/data/notes/journal/`.
- **No fabrication**: an empty day gets a short honest entry, not invented events.
- **Don't self-schedule, don't restart services.**

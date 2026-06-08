---
name: sol-daily-chronicle
description: Use when a resident asks Solilos to write, compile, or update the family journal / household chronicle / diary for a day (e.g. "schreib die Familienchronik für heute", "write today's journal", "Tagebuch-Eintrag für heute"), and as the daily 23:59 cron job that writes the day's entry unattended. Compiles the day's highlights into a standardized Obsidian-compatible Markdown file under /opt/data/notes/journal/.
version: 1.2.0
author: Solilos
license: MIT
---

# Solilos — Family Chronicle (Daily Journal)

## Overview

Compiles a day's household highlights into one standardized Markdown file in
the Syncthing-synchronized Obsidian vault, at
`/opt/data/notes/journal/journal_YYYY-MM-DD.md`. The native `qmd` skill then
indexes it, and the Obsidian Daily-Notes / calendar view picks it up on every
resident's phone.

**Two ways this runs:**
- **On request** — a resident explicitly asks for the entry (interactive).
- **Unattended daily cron** — `solbay`'s post-deploy registers a
  Hermes job (`59 23 * * *`) that fires this skill at 23:59 with no resident
  present. In that mode you must **not** ask anyone for input (see step 2).

The Honcho cross-resident aggregated-highlights extraction is still a separate,
deferred slice of #83 — do not assume it exists.

---

## When to use

- A resident asks to create or update the household journal / chronicle /
  diary for a day:
  - "Solilos, schreib die Familienchronik für heute."
  - "Write today's journal."
  - "Mach einen Tagebuch-Eintrag für heute / für den 27.05."
- When the daily cron job fires this skill at 23:59 (unattended).
- Do **not** trigger on a request to write a *general* note or fact — that is
  the `media-ingestion-multimodal` / `sol-dynamic-skills` path. This skill is
  only for the dated journal/chronicle entry.

---

## Operating Sequence

### 1. Resolve the date
- Default to today (local date). If the resident named a day ("für gestern",
  "den 27.05."), use that. Format as `YYYY-MM-DD` for the filename and tags.

### 2. Gather the day's highlights (from what is available now)
Compile from the sources you actually have — do **not** fabricate events:
- **Notes added today** — scan the vault for today's ingested items, e.g. with
  the `terminal` tool:
  `grep -rl "added_at: {{date}}" /opt/data/notes/` (and `created_at:`), then
  list the digitized books/albums/documents by title.
- **Household events** you can observe or the resident mentions explicitly.
- **The day's conversations (from memory)** — recall the day's interactions
  from your memory provider (whichever is active — built-in plus the
  configured external provider) and distil them into **group-level**
  highlights. This is the household chronicle, so summarise what *the
  household* did, learned, or decided — not what any one person said.
- **This conversation** (interactive runs only) — what the resident tells you
  about the day.

> **Privacy — highlight per group, never per person.** The journal records
> **group-level aggregated highlights only**. Concretely:
> - Summarise at the **household/family group** level. Do **not** attribute a
>   highlight to a named individual, and do **not** quote or paraphrase any one
>   resident's private conversation.
> - A thing a single resident told Solilos in private goes in the journal **only**
>   if it's a genuinely household-relevant fact (e.g. "the garden was watered"),
>   stated as a group fact — never as "X said …" or "X is …".
> - When in doubt, leave it out. A thinner honest entry beats leaking one
>   resident's private chat into a shared, Syncthing-synced file every family
>   member reads.

**If there's too little for a meaningful entry:**
- *Interactive run* — ask the resident for one or two highlights rather than
  padding the file.
- *Unattended cron run* — there is **no one to ask**. Write a short, honest
  entry from the notes/events you do have; if the day is genuinely empty,
  write a minimal entry (or skip writing entirely) rather than inventing
  content or blocking on input.

### 3. Compose the entry from the standard template
Use the template below. Keep the tone warm but factual; German for a German
household.

### 4. Write to the journal folder (merge, don't clobber)
- Ensure `/opt/data/notes/journal/` exists (create it if missing).
- Target file: `/opt/data/notes/journal/journal_<date>.md`.
- **If the file already exists** (a re-run on the same day): read it with
  `view_file` and *merge* the new highlights into the existing sections —
  never overwrite an earlier entry for that day.
- Write with `write_file` (or `replace_file_content` for the merge case).
- Write only under `/opt/data/notes/journal/` — never elsewhere.

### 5. Confirm to the resident
- Example: *"Ich habe die Familienchronik für den {{date}} in
  `journal/journal_{{date}}.md` festgehalten — sie erscheint gleich in eurem
  Obsidian-Kalender."*

---

## Standard Journal Template

```markdown
---
type: journal
tags:
  - solilos/journal
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

- Leave a section out (or write *"—"*) when there is genuinely nothing for it,
  rather than inventing content.
- **Topic tag (conditional)**: if the turn context carries an active-topic line
  `[Active topic: <name> #topic/<slug>]` (a resident-invoked run from a topic
  chat), add that exact `topic/<slug>` entry to the frontmatter `tags` list so
  the entry is retrievable by topic. The slug may be hierarchical
  (e.g. `topic/projekt/wintergarten`). Omit it for the unattended 23:59 cron run
  (no active topic).
- `{{ingested_today}}` should wiki-link the day's items, e.g.
  `- [[book_dune]] — "Dune" von Frank Herbert`, so the journal joins the graph.

---

## Failure Paths & Safety Guards

- **Path sandbox**: only write under `/opt/data/notes/journal/`. Never touch
  files outside `/opt/data/notes/`.
- **No fabrication**: an empty day gets a short, honest entry — not invented
  events.
- **Don't self-schedule, don't restart services**: the daily cron is
  registered once by `solbay`'s post-deploy. This skill never creates
  or edits its own cron job, and never calls `restart_service`.
- **Privacy**: group-level aggregated highlights only — never attribute to a
  named resident, never copy a private conversation verbatim (see step 2).

---

## Verification Checklist

1. Ask Hermes: "Schreib die Familienchronik für heute."
2. Confirm `/opt/data/notes/journal/journal_<today>.md` exists (inside the
   container or at `{{DATA_DIR}}/file-share/data/notes/journal/` on the host),
   with valid `type: journal` frontmatter and the standard sections.
3. Re-run on the same day with a new highlight and confirm the entry is
   *merged*, not overwritten.
4. Ask Hermes to recall the day's journal and confirm `qmd` retrieves it.
5. Confirm the daily cron is registered: `hermes cron list` (or
   `GET /api/jobs`) shows `sol-daily-chronicle` at `59 23 * * *`. Force a
   run with `hermes cron run sol-daily-chronicle` and confirm it writes the
   entry unattended (no prompt for resident input).

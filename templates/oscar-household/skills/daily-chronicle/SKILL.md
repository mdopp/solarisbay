---
name: oscar-daily-chronicle
description: Use when a resident asks OSCAR to write, compile, or update the family journal / household chronicle / diary for a day (e.g. "schreib die Familienchronik für heute", "write today's journal", "Tagebuch-Eintrag für heute"). Compiles the day's highlights into a standardized Obsidian-compatible Markdown file under /opt/data/notes/journal/. Manual invocation only — the automatic daily trigger is a separate, not-yet-shipped slice.
version: 1.0.0
author: OSCAR
license: MIT
---

# OSCAR — Family Chronicle (Daily Journal)

## Overview

Compiles a day's household highlights into one standardized Markdown file in
the Syncthing-synchronized Obsidian vault, at
`/opt/data/notes/journal/journal_YYYY-MM-DD.md`. The native `qmd` skill then
indexes it, and the Obsidian Daily-Notes / calendar view picks it up on every
resident's phone.

**This slice is manual only.** A resident explicitly asks for the entry. The
automatic daily cron trigger and the Honcho aggregated-highlights extraction
are separate, deferred slices of #83 — do not assume they exist.

---

## When to use

- A resident asks to create or update the household journal / chronicle /
  diary for a day:
  - "OSCAR, schreib die Familienchronik für heute."
  - "Write today's journal."
  - "Mach einen Tagebuch-Eintrag für heute / für den 27.05."
- Do **not** trigger on a request to write a *general* note or fact — that is
  the `media-ingestion-multimodal` / `oscar-dynamic-skills` path. This skill is
  only for the dated journal/chronicle entry.

---

## Operating Sequence

### 1. Resolve the date
- Default to today (local date). If the resident named a day ("für gestern",
  "den 27.05."), use that. Format as `YYYY-MM-DD` for the filename and tags.

### 2. Gather the day's highlights (from what is available now)
Compile from the sources you actually have in this manual slice — do **not**
fabricate events:
- **This conversation** and what the resident tells you about the day.
- **Notes added today** — scan the vault for today's ingested items, e.g. with
  the `terminal` tool:
  `grep -rl "added_at: {{date}}" /opt/data/notes/` (and `created_at:`), then
  list the digitized books/albums/documents by title.
- **Household events** the resident mentions explicitly.

> Privacy: record **aggregated highlights**, not verbatim resident chat. Do not
> transcribe private conversations into the journal. (Cross-resident highlight
> aggregation via Honcho — privacy-reviewed — is the deferred slice of #83.)

If you have too little to write a meaningful entry, ask the resident for one or
two highlights rather than padding the file.

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
  - oscar/journal
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
- `{{ingested_today}}` should wiki-link the day's items, e.g.
  `- [[book_dune]] — "Dune" von Frank Herbert`, so the journal joins the graph.

---

## Failure Paths & Safety Guards

- **Path sandbox**: only write under `/opt/data/notes/journal/`. Never touch
  files outside `/opt/data/notes/`.
- **No fabrication**: an empty day gets a short, honest entry — not invented
  events.
- **No cron, no auto-run**: this skill runs only when a resident asks. Do not
  schedule it or call `restart_service`.
- **Privacy**: aggregated highlights only; never copy private chat verbatim.

---

## Verification Checklist

1. Ask Hermes: "Schreib die Familienchronik für heute."
2. Confirm `/opt/data/notes/journal/journal_<today>.md` exists (inside the
   container or at `{{DATA_DIR}}/file-share/data/notes/journal/` on the host),
   with valid `type: journal` frontmatter and the standard sections.
3. Re-run on the same day with a new highlight and confirm the entry is
   *merged*, not overwritten.
4. Ask Hermes to recall the day's journal and confirm `qmd` retrieves it.

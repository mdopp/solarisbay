---
name: solaris-problem-summarizer
description: Use when an admin asks Solaris to compile, update, or refresh the troubleshooting knowledge base from system logs or past admin/diagnostic chats (e.g. "fass die letzten Probleme zusammen", "update the troubleshooting KB", "summarize what broke this week"), and as the periodic cron job that distils recurring problem→solution sequences into a structured Markdown KB at /opt/data/notes/knowledge-base/troubleshooting.md unattended.
version: 1.0.0
author: Solaris
license: MIT
---

# Solaris — Problem Summarizer (troubleshooting knowledge base)

## Overview

Inspects system logs and past admin/diagnostic conversations, identifies
troubleshooting sequences (a problem, the indicators that pointed to it, and how
it was fixed), and compiles them into one structured Markdown knowledge base at
`/opt/data/notes/knowledge-base/troubleshooting.md`. The KB lives in the
Syncthing-synced Obsidian vault, so the native `qmd` skill indexes it and it
shows up on every admin's phone, and `solaris-notes-search` can retrieve from it.

This is a **write** skill for one specific file. It complements the read-only
diagnostic skills (`solaris-status`, `solaris-audit-query`) — they observe the live
system; this one *remembers* how past problems were solved.

**Two ways this runs:**
- **On request** — an admin explicitly asks to summarize problems / update the
  KB (interactive).
- **Unattended cron** — `solarisbay`'s post-deploy registers a cron job
  (`30 4 * * 1`, Mondays 04:30) that fires this skill weekly with no admin
  present. In that mode you must **not** ask anyone for input (see step 4).

## When to use

- An admin asks to compile or update the troubleshooting knowledge base:
  - "Solaris, fass die letzten Probleme und Lösungen zusammen."
  - "Update the troubleshooting KB."
  - "Was ist diese Woche kaputtgegangen und wie haben wir es gefixt?"
- When the weekly cron job fires this skill (unattended).

Out of scope:
- Live system status / health → `solaris-status`.
- One-off cloud-audit queries → `solaris-audit-query`.
- General notes / facts / journal → the write skills
  (`media-ingestion-multimodal`, `solaris-dynamic-skills`, `solaris-daily-chronicle`).
- Acting on a problem (restarting a service, re-pairing a key) → that is the
  admin-soul skill pack, not this one. This skill only *records* solutions.

## Operating sequence

### 1. Gather troubleshooting signal from what is available now
Compile only from sources you actually have — do **not** invent problems:
- **System logs** — recent error/warn lines from the stack. Use the `terminal`
  tool, e.g. `podman logs --since 168h solaris-chat 2>&1 | grep -iE "error|warn|fail"`
  for the chat container, and the equivalent for other Solaris containers
  (`solaris-gatekeeper`, `ollama`, …). Look for *resolved* sequences: an error followed
  later by a quiet period or an explicit fix.
- **Past admin/diagnostic conversations (from memory)** — recall diagnostic
  threads from your memory provider where a problem was reported, investigated,
  and resolved. These often hold the actual fix the logs don't.

### 2. Extract problem → indicators → solution triples
For each distinct troubleshooting sequence, distil:
- **Problem** — what failed, stated as a symptom ("HA could not be reached").
- **Indicators** — the diagnostic signs that identified it ("Ping works, key is
  rejected").
- **Solution** — what fixed it, with the concrete command/script when known
  ("Re-pair the key by running x.y.z; script `a-b-c.sh` helps").

Merge near-duplicates: the same recurring problem is **one** entry, not one per
occurrence. Skip noise (a single transient warning that self-cleared with no
action) — the KB is for problems worth remembering.

### 3. Compose entries from the standard template
Use the entry template below, one block per problem. Keep it factual and
terse; German for a German household, but keep command/file names verbatim.

### 4. Write to the knowledge-base file (merge, don't clobber)
- Ensure `/opt/data/notes/knowledge-base/` exists (create it if missing).
- Target file: `/opt/data/notes/knowledge-base/troubleshooting.md`.
- **If the file already exists** (the normal case after the first run): read it
  with `notes_read` and *merge* — update an existing problem's entry in place
  when you have new indicators/solution detail, append genuinely new problems,
  and never drop or overwrite an entry you don't have a reason to change.
- Write with `note_write` (first run) or `note_write` with `append=true` (the merge
  case). Write **only** this one file.

**If there's too little for a meaningful update:**
- *Interactive run* — tell the admin there's nothing new worth recording rather
  than padding the KB.
- *Unattended cron run* — there is **no one to ask**. If nothing new surfaced,
  leave the file untouched (or write the header only on a first empty run)
  rather than inventing content or blocking on input.

### 5. Confirm (interactive runs)
- Example: *"Ich habe die Troubleshooting-Wissensdatenbank in
  `knowledge-base/troubleshooting.md` aktualisiert — N neue Einträge."*

## Standard entry template

```markdown
---
type: knowledge-base
tags:
  - solaris/troubleshooting
updated_at: {{timestamp}}
---

# Solaris — Troubleshooting Knowledge Base

## {{problem_title}}
- **Problem**: {{what_failed}}
- **Indicators**: {{diagnostic_signs}}
- **Solution**: {{how_it_was_fixed}}
```

- Keep the single `# Solaris — Troubleshooting Knowledge Base` heading and the
  frontmatter at the top; append each problem as its own `## ` section.
- Bump `updated_at` on every write.

## Guards

- **Path sandbox**: only write `/opt/data/notes/knowledge-base/troubleshooting.md`.
  Never touch any other file, inside or outside `/opt/data/notes/`.
- **No fabrication**: a quiet week gets no new entries — not invented problems.
- **No acting on problems**: this skill *records* solutions; it never runs the
  fix, restarts a service, or mutates the system. Reading logs is fine; acting
  on them is the admin path.
- **Don't self-schedule**: the weekly cron is registered once by `solarisbay`'s
  post-deploy. This skill never creates or edits its own cron job.
- **Privacy**: record the technical problem/solution, not who reported it or any
  unrelated content from a diagnostic chat.

## Failure paths

- `/opt/data/notes` unreadable/unwritable → "Die Wissensdatenbank ist gerade
  nicht erreichbar." Don't lose the analysis silently.
- No log access (container not running, `podman` denied) → summarize from memory
  alone and note in the confirmation that logs weren't available this run.

## Verification checklist

1. Ask Solaris: "Fass die letzten Probleme und Lösungen zusammen."
2. Confirm `/opt/data/notes/knowledge-base/troubleshooting.md` exists (inside
   the container or at `{{DATA_DIR}}/file-share/data/notes/knowledge-base/` on
   the host), with valid `type: knowledge-base` frontmatter and at least one
   `## ` problem block in the Problem/Indicators/Solution shape.
3. Re-run and confirm entries are *merged*, not duplicated/overwritten.
4. Confirm the weekly cron is registered in the chat admin UI — shows
   `solaris-problem-summarizer` at `30 4 * * 1`. Trigger a manual run from the
   admin UI and confirm it updates the KB unattended (no prompt for admin input).

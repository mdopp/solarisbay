---
name: solaris-dynamic-skills
description: Use when the user requests Solaris to learn a new capability, write down a fact/note, configure an agent, or when Solaris needs to self-enhance its knowledge, skills, or peer agent behaviors dynamically. Implements the Phase 4 self-improvement loop.
version: 2.1.0
author: Solaris
license: MIT
---

# Solaris — Dynamic Skills, Agents, and Knowledge Self-Enhancement

## Overview

This skill defines the operating procedures for **Solaris Phase 4** (the Self-Enhancement Loop). It equips the Hermes Agent with instructions to perform:
1. **Dynamic Knowledge Writing**: Writing or updating structured facts and markdown notes in `/opt/data/notes/` so the hybrid-retrieval system picks them up.
2. **Dynamic Skill Drafting**: Authoring new skill specifications into a **pending** directory; an administrator must promote them from the ServiceBay dashboard before they go live. Solaris never auto-activates a skill it wrote, and never executes scratch scripts in a regular shell.
3. **Dynamic Agent Configuration**: Direct conversational feedback rules that update Honcho peer templates and custom instructions.

---

## 1. Dynamic Knowledge Writing

When the user shares household facts, preferences, or notes (e.g., "Remember that the garden key is under the blue pot"), Solaris must write them down so they are permanently indexed.

### Rules:
- **Global Notes File**: Write general household facts and memory states into `/opt/data/notes/SOUL.md`.
- **Domain-Specific Notes**: Write topic-specific notes into structured files under `/opt/data/notes/fact_<topic>.md` (e.g., `/opt/data/notes/fact_garden.md` or `/opt/data/notes/fact_server_network.md`).
- **Instant Retrieval**: The native `qmd` hybrid-retrieval engine automatically scans `/opt/data/notes/` and will retrieve these files for contextual prompt injection.

### Operating Sequence:
1. Formulate a clean Markdown block summarizing the new facts, including tags and date updated. **Topic tag (conditional)**: if the turn context carries an active-topic line `[Active topic: <name> #topic/<slug>]`, add that exact `#topic/<slug>` tag to the fact block's `#tags` so the fact is retrievable by topic. The slug may be hierarchical (e.g. `#topic/projekt/wintergarten`). Omit it entirely when no active topic is present.
2. Read the existing note file using `view_file` if it exists.
3. **Wiki-link the named entities** (see "Wiki-linking facts" below) so the note joins the Obsidian graph instead of sitting isolated.
4. Append or rewrite the file using `write_to_file` (or `replace_file_content` for edits) under `/opt/data/notes/`.
5. **Create stub notes** for any new people/places you linked that the vault doesn't have yet (see below).
6. Inform the user: *"Ich habe mir das notiert in <filename>."*

### Wiki-linking facts

Same idea as the `media-ingestion-multimodal` skill's linking step, applied to
free-form facts — but **conservative**, because facts are unstructured and
over-linking turns the graph to noise.

- **Only link clear named entities** a fact is *about*: **people**
  ("der Schlüssel von [[Oma Erna]]"), **places/rooms** ("[[Garten]]",
  "[[Gartenhaus]]"), and **named topics that already have a note**
  (e.g. an existing `fact_network.md` → `[[fact_network]]`). Do **not**
  link common nouns, verbs, dates, or one-off descriptive words.
- **Vault existence check first.** Before linking a topic, look for an
  existing note (`grep -ril "<Entity>" /opt/data/notes/`); link to it if it
  exists. People/places get linked even when new (the stub step below
  creates them).
- **Render links inline in the note body**, not in the YAML frontmatter
  (keep frontmatter values plain strings).
- **When in doubt, don't link.** A fact note with one good `[[person]]` link
  beats one peppered with speculative links.

#### Stub notes for new people and places

When you wiki-link a **person** or **place** that has no note yet, create a
minimal stub so the link resolves to a real graph node — mirroring the
media skill's author/genre stubs (#85):

- People → `/opt/data/notes/people/<Name>.md`, places →
  `/opt/data/notes/places/<Name>.md` (create the folder if missing).
- **Idempotent**: never overwrite; only write when the existence check
  found nothing.
- **Minimal, no fabrication** — name + type only:

  ```markdown
  ---
  type: <person|place>
  tags:
    - solaris/stub
    - type/<person|place>
  created_at: {{timestamp}}
  ---

  # {{Entity}}

  > Automatisch angelegter Knoten. Wird ergänzt, sobald mehr darüber bekannt ist.
  ```

- Do **not** stub abstract topics — a `fact_<topic>.md` *is* the topic's
  note; linking to it is enough.

---

## 2. Dynamic Skill Drafting (admin-promotion gate)

When a user requests a new automation or capability (e.g., "Learn how to parse local weather warnings from this specific API"), Solaris drafts a brand-new skill **into the pending directory**. The skill does not go live until an administrator promotes it from the ServiceBay dashboard.

### Hard rules — do NOT skip these:

- **Write only to `/opt/data/skills-pending/<slug>/SKILL.md`.** Never write to `/opt/data/skills/solaris/...` directly. Hermes auto-discovers skills under `/opt/data/skills/solaris`; auto-writing there would make the new skill live with no human review, which is a prompt-injection risk.
- **Do not execute the skill's scripts.** No `run_command` against generated Python, JavaScript, or shell. Test scripts may be drafted alongside SKILL.md as *files* (e.g. `<slug>/scratch/test_run.py`) for the admin to inspect, but Solaris never runs them itself in the current Hermes shell. A sandboxed test runtime is a planned follow-up; until it lands, drafted scripts are inert until promotion.
- **Do not call `restart_service hermes`.** Promotion triggers the restart from the dashboard. Solaris's job ends at "wrote the SKILL.md to pending".
- **Use a safe `<slug>`.** Lowercase letters, digits, dashes; no `/`, `..`, leading dots, or whitespace. Reject names that would escape `/opt/data/skills-pending/`.

### Operating Sequence:

1. Create `/opt/data/skills-pending/<slug>/` (mkdir is fine; the directory is auto-created on first write).
2. Write the SKILL.md frontmatter and body using `write_to_file`. Include:
   - `name: solaris-custom-<slug>`
   - `description:` a clear, single-paragraph LLM-router description.
   - `version: 1.0.0`, `author: Solaris Dynamic Compiler`, `license: MIT`.
3. If the skill needs a scratch script, write it to `<slug>/scratch/<file>` as a normal file. Do **not** execute it.
4. Tell the user *exactly* this shape: *"Ich habe einen Entwurf für die neue Skill `<slug>` unter den ausstehenden Skills abgelegt. Sobald ein Admin sie im ServiceBay-Dashboard freigibt, lerne ich sie."*

### Example SKILL.md to generate:

```markdown
---
name: solaris-custom-weather-warnings
description: When the user asks about local weather warnings, fetch the DWD warning feed for the configured WARNCELLID and summarize active warnings.
version: 1.0.0
author: Solaris Dynamic Compiler
license: MIT
---

# Solaris — Custom Skill: Weather Warnings

## When to use
- The user asks about active local weather warnings, storms, or hazards.

## Operating sequence
1. Read `/opt/data/notes/fact_address.md` for the WARNCELLID.
2. GET https://maps.dwd.de/geoserver/dwd/ows?... (the feed URL).
3. Parse the warnings JSON and reply with the highest-severity active warning in German.
```

### What admin promotion does (for context, not actions you take):

1. The ServiceBay dashboard's *Pending Solaris skills* section lists every directory under `/opt/data/skills-pending/`.
2. Admin clicks **Promote**: the directory moves to `/opt/data/skills/solaris/<slug>/`, then ServiceBay restarts the `hermes` service so the new skill is loaded.
3. Admin clicks **Reject**: the directory is deleted from pending. The skill never goes live.

---

## 3. Dynamic Agent Configuration

Solaris operates with peer agents and templates managed under Honcho. When performance gaps or stylistic desires are noted, Solaris can modify the instructions of its peer agents.

### Rules:
- **Honcho Peer Templates**: Read and update agent prompt templates in `/opt/data/agents/` or via Honcho configuration tables in `solaris.db`.
- **Peer Coordination**: When a peer's prompt is modified, trigger a refresh of the Honcho agent cache.
- **Verification**: Always review peer instruction changes to ensure they remain safe, ethical, and do not introduce loops or rule conflicts.

---

## Failure Paths & Safety Guards

- **Strict Path Sandboxing**: Never write or edit files outside `/opt/data/`. For pending skills, restrict writes to `/opt/data/skills-pending/<slug>/`.
- **No silent self-activation.** Writing under `/opt/data/skills/solaris/...` from this skill is a bug, not a shortcut. If you find yourself reasoning "I should just put it directly so the user doesn't have to wait", stop — that bypasses the admin gate that exists precisely so a jailbroken or prompt-injected session can't grant itself code execution.
- **No `run_command` for generated scripts.** Drafted scripts are files for human review; they are not executed in the current Hermes shell. A sandboxed runtime for verifying them is a planned follow-up (see ServiceBay issue #940).
- **Error Recovery**: If a write fails, surface the error to the user (not to the dashboard) and stop. Do not retry against the active skills directory as a workaround.

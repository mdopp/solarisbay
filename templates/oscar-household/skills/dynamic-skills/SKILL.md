---
name: oscar-dynamic-skills
description: Use when the user requests OSCAR to learn a new capability, write down a fact/note, configure an agent, or when OSCAR needs to self-enhance its knowledge, skills, or peer agent behaviors dynamically. Implements the Phase 4 self-improvement loop.
version: 2.0.0
author: OSCAR
license: MIT
---

# OSCAR — Dynamic Skills, Agents, and Knowledge Self-Enhancement

## Overview

This skill defines the operating procedures for **OSCAR Phase 4** (the Self-Enhancement Loop). It equips the Hermes Agent with instructions to perform:
1. **Dynamic Knowledge Writing**: Writing or updating structured facts and markdown notes in `/opt/data/notes/` so the hybrid-retrieval system picks them up.
2. **Dynamic Skill Drafting**: Authoring new skill specifications into a **pending** directory; an administrator must promote them from the ServiceBay dashboard before they go live. OSCAR never auto-activates a skill it wrote, and never executes scratch scripts in a regular shell.
3. **Dynamic Agent Configuration**: Direct conversational feedback rules that update Honcho peer templates and custom instructions.

---

## 1. Dynamic Knowledge Writing

When the user shares household facts, preferences, or notes (e.g., "Remember that the garden key is under the blue pot"), OSCAR must write them down so they are permanently indexed.

### Rules:
- **Global Notes File**: Write general household facts and memory states into `/opt/data/notes/SOUL.md`.
- **Domain-Specific Notes**: Write topic-specific notes into structured files under `/opt/data/notes/fact_<topic>.md` (e.g., `/opt/data/notes/fact_garden.md` or `/opt/data/notes/fact_server_network.md`).
- **Instant Retrieval**: The native `qmd` hybrid-retrieval engine automatically scans `/opt/data/notes/` and will retrieve these files for contextual prompt injection.

### Operating Sequence:
1. Formulate a clean Markdown block summarizing the new facts, including tags and date updated.
2. Read the existing note file using `view_file` if it exists.
3. Append or rewrite the file using `write_to_file` (or `replace_file_content` for edits) under `/opt/data/notes/`.
4. Inform the user: *"Ich habe mir das notiert in <filename>."*

---

## 2. Dynamic Skill Drafting (admin-promotion gate)

When a user requests a new automation or capability (e.g., "Learn how to parse local weather warnings from this specific API"), OSCAR drafts a brand-new skill **into the pending directory**. The skill does not go live until an administrator promotes it from the ServiceBay dashboard.

### Hard rules — do NOT skip these:

- **Write only to `/opt/data/skills-pending/<slug>/SKILL.md`.** Never write to `/opt/data/skills/oscar/...` directly. Hermes auto-discovers skills under `/opt/data/skills/oscar`; auto-writing there would make the new skill live with no human review, which is a prompt-injection risk.
- **Do not execute the skill's scripts.** No `run_command` against generated Python, JavaScript, or shell. Test scripts may be drafted alongside SKILL.md as *files* (e.g. `<slug>/scratch/test_run.py`) for the admin to inspect, but OSCAR never runs them itself in the current Hermes shell. A sandboxed test runtime is a planned follow-up; until it lands, drafted scripts are inert until promotion.
- **Do not call `restart_service hermes`.** Promotion triggers the restart from the dashboard. OSCAR's job ends at "wrote the SKILL.md to pending".
- **Use a safe `<slug>`.** Lowercase letters, digits, dashes; no `/`, `..`, leading dots, or whitespace. Reject names that would escape `/opt/data/skills-pending/`.

### Operating Sequence:

1. Create `/opt/data/skills-pending/<slug>/` (mkdir is fine; the directory is auto-created on first write).
2. Write the SKILL.md frontmatter and body using `write_to_file`. Include:
   - `name: oscar-custom-<slug>`
   - `description:` a clear, single-paragraph LLM-router description.
   - `version: 1.0.0`, `author: OSCAR Dynamic Compiler`, `license: MIT`.
3. If the skill needs a scratch script, write it to `<slug>/scratch/<file>` as a normal file. Do **not** execute it.
4. Tell the user *exactly* this shape: *"Ich habe einen Entwurf für die neue Skill `<slug>` unter den ausstehenden Skills abgelegt. Sobald ein Admin sie im ServiceBay-Dashboard freigibt, lerne ich sie."*

### Example SKILL.md to generate:

```markdown
---
name: oscar-custom-weather-warnings
description: When the user asks about local weather warnings, fetch the DWD warning feed for the configured WARNCELLID and summarize active warnings.
version: 1.0.0
author: OSCAR Dynamic Compiler
license: MIT
---

# OSCAR — Custom Skill: Weather Warnings

## When to use
- The user asks about active local weather warnings, storms, or hazards.

## Operating sequence
1. Read `/opt/data/notes/fact_address.md` for the WARNCELLID.
2. GET https://maps.dwd.de/geoserver/dwd/ows?... (the feed URL).
3. Parse the warnings JSON and reply with the highest-severity active warning in German.
```

### What admin promotion does (for context, not actions you take):

1. The ServiceBay dashboard's *Pending OSCAR skills* section lists every directory under `/opt/data/skills-pending/`.
2. Admin clicks **Promote**: the directory moves to `/opt/data/skills/oscar/<slug>/`, then ServiceBay restarts the `hermes` service so the new skill is loaded.
3. Admin clicks **Reject**: the directory is deleted from pending. The skill never goes live.

---

## 3. Dynamic Agent Configuration

OSCAR operates with peer agents and templates managed under Honcho. When performance gaps or stylistic desires are noted, OSCAR can modify the instructions of its peer agents.

### Rules:
- **Honcho Peer Templates**: Read and update agent prompt templates in `/opt/data/agents/` or via Honcho configuration tables in `oscar.db`.
- **Peer Coordination**: When a peer's prompt is modified, trigger a refresh of the Honcho agent cache.
- **Verification**: Always review peer instruction changes to ensure they remain safe, ethical, and do not introduce loops or rule conflicts.

---

## Failure Paths & Safety Guards

- **Strict Path Sandboxing**: Never write or edit files outside `/opt/data/`. For pending skills, restrict writes to `/opt/data/skills-pending/<slug>/`.
- **No silent self-activation.** Writing under `/opt/data/skills/oscar/...` from this skill is a bug, not a shortcut. If you find yourself reasoning "I should just put it directly so the user doesn't have to wait", stop — that bypasses the admin gate that exists precisely so a jailbroken or prompt-injected session can't grant itself code execution.
- **No `run_command` for generated scripts.** Drafted scripts are files for human review; they are not executed in the current Hermes shell. A sandboxed runtime for verifying them is a planned follow-up (see ServiceBay issue #940).
- **Error Recovery**: If a write fails, surface the error to the user (not to the dashboard) and stop. Do not retry against the active skills directory as a workaround.

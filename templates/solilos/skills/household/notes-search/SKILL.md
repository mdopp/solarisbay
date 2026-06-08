---
name: sol-notes-search
description: Use when a resident asks Solilos to find, search, recall, or look something up in the household notes / knowledge base / Obsidian vault (e.g. "what did we note about the garden?", "find the book I added about X", "wo hab ich das mit dem WLAN-Passwort notiert?", "such in meinen Notizen nach …"), or to show everything about a topic/project (e.g. "zeig mir alles zu Projekt Wintergarten", "was haben wir zu <Thema>", "show me everything about <topic>"). Retrieves matching notes from the Syncthing-synced vault under /opt/data/notes and feeds them back into the answer; a topic request filters by the `#topic/<slug>` tag. Read-only.
version: 1.1.0
author: Solilos
license: MIT
---

# Solilos — Notes Search (knowledge-base retrieval)

## Overview

On-demand retrieval over the household's Obsidian notes vault
(`/opt/data/notes`, Syncthing-synced). This is the **read half** of Solilos's
knowledge base — the write half is `media-ingestion-multimodal` (book/album/
document notes), `sol-dynamic-skills` (facts in `SOUL.md` / `fact_*.md`), and
`sol-daily-chronicle` (journal). This skill lets the agent *find* and cite
what was written.

Retrieval is **keyword + frontmatter** search via `ripgrep` over the markdown —
fast, local, no index to maintain. It is **not** semantic vector search; for
"find notes about this *concept*" the local `qmd` engine is the upgrade path,
but it is a separate heavyweight install (Node ≥22 + ~2 GB of GGUF models) and
not part of this skill.

## When to use

- "Was haben wir über den Garten notiert?" / "What did we note about the boiler?"
- "Find the book I added about Roman history."
- "Wo steht das WLAN-Passwort?" / "Where did I write down the spare-key spot?"
- "Search my notes for <topic>."
- As the **retrieval step** before answering any question that the household
  notes might already answer — check the vault before saying "I don't know".

Out of scope:
- Writing/updating notes → `sol-dynamic-skills` / `media-ingestion-multimodal`.
- The dated journal → `sol-daily-chronicle`.
- Conversation history → that's Hermes' memory provider, not the notes vault.

## Operating sequence

1. **Derive search terms** from the request: the key nouns/entities, plus
   obvious synonyms and the German/English variant (the vault is bilingual).
2. **Search the vault** with `ripgrep` via the `terminal` tool. Case-insensitive,
   list matching files first, then with context:
   ```bash
   # filenames + frontmatter (titles, tags, author, type) and body:
   rg -il "<term>" /opt/data/notes/
   # then pull a few lines of context from the top hits:
   rg -i -n -C2 "<term>" /opt/data/notes/<hit>.md
   ```
   Also try the filename convention directly (`book_*`, `album_*`, `fact_*`,
   `journal/journal_*`, `authors/`, `people/`, `places/`) when the request
   names an entity.
   - **Filter by topic** when the resident asks for *everything about a
     topic/project* ("zeig mir alles zu Projekt Wintergarten", "was haben wir
     zu X"). Ingestion stamps each note from a topic-chat with a `#topic/<slug>`
     tag (the slug is lower-case, hierarchical, e.g.
     `topic/projekt/wintergarten`). Slugify the named topic (lower-case, spaces
     → `-`, the hierarchy joined by `/`) and grep the tag — it is written in
     two forms, a frontmatter list entry `topic/<slug>` (without the `#`) and an
     inline `#topic/<slug>` token, so match both:
     ```bash
     # all notes for the topic (either tag form), for this resident:
     rg -il "#?topic/projekt/wintergarten\b" /opt/data/notes/
     ```
     This is a *required* filter for a topic request — return the tagged set,
     not a fuzzy keyword match. The slug boundary matters: `projekt/wintergarten`
     must not pull in `projekt/wintergartendach` or a child `…/wintergarten/glas`.
     If the resident isn't sure of the exact name, fall back to the keyword
     search above. (The chat panel's topic dashboard surfaces the same set via
     `GET /api/topics/<slug>/items`.)
3. **Rank + pick** the most relevant 1–5 notes. Prefer frontmatter/title hits
   over incidental body mentions. For a topic request, list the matching notes
   rather than collapsing to one.
4. **Read the chosen notes** with `view_file` to get the full content.
5. **Answer from them**, and say which note(s) it came from (the filename or
   the wiki-link, e.g. "steht in `fact_garden.md`"). Don't read UUIDs/hashes
   aloud.

## Guards

- **Read-only.** This skill only reads `/opt/data/notes`. Never write, move, or
  delete there (that's the write skills' job).
- **Stay in the vault.** Only search under `/opt/data/notes`. Don't grep the
  whole filesystem.
- **No fabrication.** If nothing matches, say so plainly ("Dazu hab ich nichts
  in den Notizen gefunden.") rather than inventing an answer. Offer to write a
  note via the ingestion/dynamic-skills path if the resident wants to record it.
- **Privacy.** Surface what the vault holds, but don't read out whole private
  documents verbatim unless asked — summarise and point to the note.

## Failure paths

- `/opt/data/notes` empty or unreadable → "Meine Notizen sind gerade leer/nicht
  erreichbar." (Note-writing is restored once the vault is writable — see #81.)
- Too many hits → narrow with a more specific term or combine terms; report the
  top few and offer to refine, don't dump everything.

## Related

- Write paths: `media-ingestion-multimodal`, `sol-dynamic-skills`,
  `sol-daily-chronicle`.
- Semantic upgrade: Hermes' native `qmd` skill (hybrid BM25 + vector + rerank)
  — needs the `@tobilu/qmd` engine + models installed in the Hermes image and
  the optional skill enabled; out of scope for this keyword retriever.

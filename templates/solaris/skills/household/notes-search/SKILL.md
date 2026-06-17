---
name: solaris-notes-search
description: Read-only keyword + frontmatter retrieval over the household Obsidian vault. Use to find/recall a note, or show everything under a topic.
kind: skill
scope: household
command: /notes
version: 2.0.0
author: Solaris
license: MIT
---

# Solaris — Notes Search (knowledge-base retrieval)

On-demand retrieval over the household's Obsidian notes vault (`/opt/data/notes`,
Syncthing-synced) — the **read half** of the knowledge base. Keyword +
frontmatter search via `ripgrep`; not semantic vector search. Read-only. Also
runnable on demand as `/notes <query>`.

## When to use

- "Was haben wir über den Garten notiert?" / "What did we note about the boiler?"
- "Find the book I added about Roman history."
- "Wo steht das WLAN-Passwort?" / "Search my notes for <topic>."
- "Zeig mir alles zu Projekt Wintergarten" / "show me everything about <topic>."
- As the **retrieval step** before answering any question the notes might already
  answer — check the vault before saying "I don't know".

Out of scope: writing notes (`solaris-dynamic-skills` /
`media-ingestion-multimodal`), the dated journal (`solaris-daily-chronicle`),
conversation history (the engine's memory provider).

## Operating sequence

1. **Derive search terms** from the request — key nouns/entities plus the
   German/English variant (the vault is bilingual).
2. **Search with `ripgrep`** via the `terminal` tool, case-insensitive:
   ```bash
   rg -il "<term>" /opt/data/notes/
   rg -i -n -C2 "<term>" /opt/data/notes/<hit>.md
   ```
   Also try the filename conventions (`book_*`, `album_*`, `fact_*`,
   `journal/journal_*`, `authors/`, `people/`, `places/`).
   - **Topic filter (required for a topic request).** Slugify the named topic
     (lower-case, spaces → `-`, hierarchy joined by `/`) and match both tag forms:
     ```bash
     rg -il "#?topic/projekt/wintergarten\b" /opt/data/notes/
     ```
     The slug boundary matters: `projekt/wintergarten` must not pull in
     `projekt/wintergartendach`. If unsure of the exact name, fall back to keywords.
3. **Rank + pick** the 1–5 most relevant notes (prefer frontmatter/title hits over
   incidental body mentions). For a topic request, list the matching set.
4. **Read the chosen notes** with `notes_read`.
5. **Answer from them** and cite the note(s) by filename/wiki-link
   ("steht in `fact_garden.md`"). Don't read UUIDs/hashes aloud.

## Guards

- **Read-only**: never write, move, or delete under `/opt/data/notes`.
- **Stay in the vault**: only search under `/opt/data/notes`.
- **No fabrication**: if nothing matches, say so plainly and offer to record it
  via the write path.
- **Privacy**: summarise and point to the note rather than reading whole private
  documents verbatim unless asked.

## Failure paths

- `/opt/data/notes` empty/unreadable → "Meine Notizen sind gerade leer/nicht
  erreichbar."
- Too many hits → narrow the term; report the top few and offer to refine.

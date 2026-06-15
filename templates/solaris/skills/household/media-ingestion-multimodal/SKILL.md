---
name: media-ingestion-multimodal
description: Triggers automatically when a resident uploads an image or photo attachment (such as a book cover, physical document, receipt, or music album art) via Signal, Telegram, Discord, or any other messaging gateway, or explicitly asks to ingest or scan a photographed item. Extracts structured metadata (ISBN, Title, Author, Artist, Tracklist, or Document Summary) and full OCR transcripts using multimodal LLMs. Writes the formatted, Obsidian-compatible structured Markdown note directly into the '/opt/data/notes/' folder (which is synced via Syncthing) so that the native 'qmd' skill can automatically index and retrieve it.
version: 1.4.0
author: Solaris
license: MIT
---

# Solaris — Multimodal Ingestion Pipeline

## Overview

Processes physical media, documents, and books sent as image attachments by family members. Extracts rich structured metadata and OCR transcriptions using a multimodal model (local Qwen-VL/LLaVA on the 16GB GPU, or opted-in cloud models), compiles the result into a beautiful, standard Markdown note in the Syncthing-synchronized `/opt/data/notes/` folder, and makes it searchable via the native `qmd` skill.

No custom database migrations or schema alterations required.

---

## When to use

- When a resident sends an image attachment of a **book cover**, **music album cover**, or **physical document page** via Signal, Telegram, or Discord.
- When a user explicitly says:
  - "Solaris, process this book picture."
  - "Nimm dieses Dokument in mein Gedächtnis auf."
  - "Füge dieses Album meiner Sammlung hinzu."
  - "Hier ist ein Foto von der Quittung."

Do **not** trigger on plain text messages that do not contain an image attachment or references to a photographic scan.

---

## Operating Sequence

### 1. Identify and Fetch Image
- Retrieve the uploaded image attachment from the messaging gateway context.
- Confirm receipt of the image to the user: "Ich habe dein Bild erhalten und analysiere es..."

### 2. Multi-Modal Analysis & OCR Extraction
- Call your multimodal LLM capability (or the `vision_analyze` tool) with the image.
- Instruct the model to perform both **OCR (text extraction)** and **Structured Metadata Extraction** with the following prompt:
  ```
  Perform optical character recognition (OCR) on this image.
  Classify the object as one of: [Book, Music Album, Document, Receipt, Other].
  Extract all relevant metadata:
  - For Books: Title, Author(s), Publisher, Year of Publication, ISBN, Language, Genre, Key Topics.
  - For Music Albums: Album Title, Artist/Band, Release Year, Genre, Tracklist (if visible).
  - For Documents/Receipts: Document Type, Subject, Date, Sender, Recipient, Key figures (like total price for receipts), Summary.
  Provide the result in clear JSON format, along with the full transcribed raw text.
  ```

### 3. Compile Premium Obsidian-Compatible Markdown
- Structure the resulting note as a standardized Markdown file.
- **YAML Frontmatter**:
  - `type`: `book`, `album`, `document`, or `receipt`
  - `tags`: `#solaris/ingested` combined with `#type/book`, `#type/album`, `#type/document`, or `#type/receipt`
  - **Topic tag (conditional)**: if the turn context carries an active-topic line `[Active topic: <name> #topic/<slug>]`, add that exact `#topic/<slug>` tag to the `tags` list (as a `topic/<slug>` entry, matching the YAML list form below) so the note is retrievable by topic. The slug may be hierarchical (e.g. `topic/projekt/wintergarten`). Omit it entirely when no active topic is present.
  - `added_by`: the current resident `uid` (default `guest` if unresolved)
  - `added_at`: current ISO-8601 timestamp
  - `isbn` / `artist` / `document_date` (specific extracted fields)
- **Document Body**:
  - `# <Title / Document Subject>`
  - Section for **Extracted Metadata** (in a clean table format)
  - Section for **Summary & Key Facts**
  - Section for **OCR Transcript** (under an expandable folding block if long)

### 3b. Insert Obsidian Wiki-Links (vault-aware)

Turn the key entities into Obsidian wiki-links so the new note joins the
graph instead of sitting isolated. Applies to the structured-metadata
fields — **author(s)**, **genre(s)**, **artist** — and any **related works**
you extracted.

1. **List candidate link targets** from the metadata: each author, each
   genre, the artist (albums), and any explicitly named related titles.
2. **Vault existence check** — for each candidate, look for an existing note
   before linking. Search `/opt/data/notes/` recursively, e.g. with the
   `terminal` tool: `grep -ril "<Entity>" /opt/data/notes/` or
   `ls /opt/data/notes/**/"<Entity>".md`. A hit means the target note
   already exists; no hit means it's new.
3. **Write the link either way.** Obsidian renders `[[Entity]]` whether or
   not the target file exists yet (a missing target shows as an unresolved
   link that resolves the moment the note is created). So always emit the
   wiki-link.
4. **Render the links in the document body's metadata block** (not in the
   YAML frontmatter — keep frontmatter values plain strings):
   - `**Author:** [[Frank Herbert]]`
   - `**Genre:** [[Science Fiction]]`
   - `**Related:** [[Dune Messiah]], [[Children of Dune]]`

### 3c. Create stub notes for new authors, artists, and genres

So a wiki-linked author, artist, or genre becomes a real, browsable graph
node (not a dangling unresolved link), create a **minimal stub note** for
each **author**, **artist**, and **genre** candidate from 3b that the vault
existence check found **no** existing note for. This keeps the vault tidy and
lets backlinks accumulate on that entity over time.

1. **Only authors, artists, and genres get auto-stubs.** Do **not** auto-stub
   related works (other book/album titles) — those become real notes when the
   resident actually ingests them; stubbing every mentioned title would
   litter the vault with orphan nodes. Their `[[…]]` links stay unresolved
   until then, which is fine.
2. **Folder convention** — author stubs go in `/opt/data/notes/authors/`,
   artist stubs in `/opt/data/notes/artists/`, genre stubs in
   `/opt/data/notes/genres/`. Create the folder if it's missing. Obsidian's
   link resolver still matches `[[Frank Herbert]]` to
   `authors/Frank Herbert.md` by basename, so the links from 3b resolve.
3. **Filename** — `<Entity>.md` with the entity's display name (sanitize only
   path-unsafe characters `/` and `\`; keep spaces and capitalisation so the
   basename matches the `[[Entity]]` link).
4. **Idempotent** — never overwrite. If the existence check in 3b found a note
   anywhere in the vault, skip; only `note_write` when the target is genuinely
   absent.
5. **Minimal content, no fabrication** — write only the name and type; do not
   invent biography, discography, or dates. Use the stub template below.

#### Author / artist / genre stub template

```markdown
---
type: <author|artist|genre>
tags:
  - solaris/stub
  - type/<author|artist|genre>
created_at: {{timestamp}}
---

# {{Entity}}

> Automatisch angelegter Knoten. Wird ergänzt, sobald mehr darüber bekannt ist.
```

Then continue to step 4 and write the ingested item's own note as usual — its
`[[Author]]` / `[[Artist]]` / `[[Genre]]` links now resolve to these stubs.

### 4. Write Markdown to the Sync Folder
- Create a safe, sanitized filename to avoid name collisions:
  - Books: `book_<sanitized_title>.md`
  - Albums: `album_<sanitized_artist>_<sanitized_title>.md`
  - Documents: `doc_<sanitized_subject>_<date>.md`
- Write the compiled Markdown note into `/opt/data/notes/<filename>` using `note_write`.
- Ensure the `/opt/data/notes` parent directory is created if not already present.

### 5. Proactive Resident Confirmation
- Summarize the extraction results to the user in a natural, premium tone.
- **Example**:
  > "Ich habe das Buch '**Dune**' von **Frank Herbert** (ISBN: 978-0441172719) erkannt und als Notiz `book_dune.md` in deinen Syncthing-Notizen abgelegt. Es wurde sofort indexiert und steht dir im Langzeitgedächtnis zur Verfügung."

### 6. Automatic Indexing
- The native `qmd` skill periodically (or on reload) scans `/opt/data/notes/` using its BM25 hybrid-retrieval engine. The note is now searchable across all channels!

---

## Obsidian-Compatible Note Templates

### Book Template
```markdown
---
type: book
tags:
  - solaris/ingested
  - type/book
added_by: {{uid}}
added_at: {{timestamp}}
isbn: "{{isbn}}"
title: "{{title}}"
author: "{{author}}"
publisher: "{{publisher}}"
year: {{year}}
---

# {{title}}

## Metadaten
| Feld | Wert |
|---|---|
| **Titel** | {{title}} |
| **Autor** | [[{{author}}]] |
| **Verlag** | {{publisher}} |
| **Jahr** | {{year}} |
| **ISBN** | {{isbn}} |

## Inhaltszusammenfassung
{{summary}}

## Roher Text (OCR)
```
{{ocr_text}}
```
```

### Music Album Template
```markdown
---
type: album
tags:
  - solaris/ingested
  - type/album
added_by: {{uid}}
added_at: {{timestamp}}
album_title: "{{title}}"
artist: "{{artist}}"
year: {{year}}
genre: "{{genre}}"
---

# {{title}} — {{artist}}

## Album-Details
| Feld | Wert |
|---|---|
| **Album** | {{title}} |
| **Künstler** | [[{{artist}}]] |
| **Jahr** | {{year}} |
| **Genre** | [[{{genre}}]] |

## Trackliste
{{tracklist}}

## Roher Text (OCR)
```
{{ocr_text}}
```
```

### Document/Receipt Template
```markdown
---
type: {{doc_type}}
tags:
  - solaris/ingested
  - type/{{doc_type}}
added_by: {{uid}}
added_at: {{timestamp}}
doc_date: "{{doc_date}}"
subject: "{{subject}}"
---

# {{subject}} ({{doc_date}})

## Dokumenten-Details
| Feld | Wert |
|---|---|
| **Typ** | {{doc_type}} |
| **Datum** | {{doc_date}} |
| **Betreff** | {{subject}} |

## Wichtige Fakten & Beträge
{{facts}}

## Roher Text (OCR)
```
{{ocr_text}}
```
```

---

## Verification Checklist

To verify correctness:
1. Upload an image of a book cover or document page via Signal or Telegram.
2. Verify that `media-ingestion-multimodal` triggers and correctly runs the vision analysis.
3. Check `/opt/data/notes/` inside the container or `{{DATA_DIR}}/file-share/data/notes` on the host to ensure the Markdown file exists and is populated correctly.
4. Ask Solaris: "Erzähl mir von dem Buch, das ich heute hinzugefügt habe" and confirm the native `qmd` skill retrieves it.

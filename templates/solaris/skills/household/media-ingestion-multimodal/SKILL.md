---
name: media-ingestion-multimodal
description: On an image upload, OCR + extract structured metadata and write an Obsidian-compatible note into the vault.
kind: hook
scope: household
event: image-upload
version: 2.0.0
author: Solaris
license: MIT
---

# Solaris — Multimodal Ingestion Pipeline

**Binds:** `image-upload` (a resident sends a book cover, album art, document or
receipt as an image attachment)

OCR and extract structured metadata from the image, then write a standard
Markdown note into the Syncthing-synced `/opt/data/notes/` folder so the native
`qmd` skill indexes it. Do not run on plain text with no image.

## What to do on the event

### 1. Fetch the image
Retrieve the uploaded attachment from the gateway context. Confirm receipt:
"Ich habe dein Bild erhalten und analysiere es…"

### 2. OCR + structured extraction
Call your multimodal capability (or `vision_analyze`) on the image and ask for
both OCR and structured metadata as JSON:

```
Perform optical character recognition (OCR) on this image.
Classify the object as one of: [Book, Music Album, Document, Receipt, Other].
Extract all relevant metadata:
- Books: Title, Author(s), Publisher, Year, ISBN, Language, Genre, Key Topics.
- Music Albums: Album Title, Artist/Band, Release Year, Genre, Tracklist.
- Documents/Receipts: Type, Subject, Date, Sender, Recipient, Key figures, Summary.
Provide the result in clear JSON, plus the full transcribed raw text.
```

### 3. Compile the note + wiki-links
Build the note from the matching template below. Frontmatter `type` =
`book`/`album`/`document`/`receipt`; `tags` = `solaris/ingested` + `type/<type>`;
`added_by` = the resident uid (default `guest`); `added_at` = ISO timestamp.
If the turn context carries `[Active topic: <name> #topic/<slug>]`, add that
`topic/<slug>` to `tags` (slug may be hierarchical); omit otherwise.

In the **body** (not the frontmatter), turn authors, genres, and the artist into
Obsidian wiki-links (`[[Frank Herbert]]`, `[[Science Fiction]]`). Always emit the
link; Obsidian resolves it when the target exists. For each **author/artist/genre**
that has no existing vault note (check with `grep -ril "<Entity>" /opt/data/notes/`),
create a minimal stub note (authors→`authors/`, artists→`artists/`, genres→
`genres/`; filename `<Entity>.md`, keep spaces/case) so the link resolves to a real
node. Do **not** auto-stub related works. Never overwrite an existing note; no
invented biography/discography/dates.

#### Stub template
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

### 4. Write the note
Sanitized filename: books `book_<title>.md`, albums
`album_<artist>_<title>.md`, documents `doc_<subject>_<date>.md`. Write into
`/opt/data/notes/<filename>` with `note_write` (create the folder if missing).

### 5. Confirm
Summarize naturally, e.g. "Ich habe das Buch '**Dune**' von **Frank Herbert**
erkannt und als `book_dune.md` abgelegt — es ist jetzt im Langzeitgedächtnis."

## Note templates

### Book
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
{{ocr_text}}
```

### Music album
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
{{ocr_text}}
```

### Document / receipt
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
{{ocr_text}}
```

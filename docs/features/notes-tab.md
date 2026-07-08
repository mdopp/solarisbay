# Notizen tab

`/p/notes` is the human-in-the-loop surface for the household knowledge vault:
browse it, search it, read and edit notes, see stats, and curate the inbox of
loose facts the nightly [Bibliothekar](knowledge-system.md) would otherwise
consolidate on its own.

<!-- screenshot: /p/notes -->

## What it does

- **Overview** — counts (notes / facts / inbox), the 10 most-recently-modified
  notes, and the tail of the Bibliothekar's change log (`notes/okf/log.md`).
- **Durchstöbern** — browse the vault grouped by `topic`, `person`, `journal`,
  `okf`, or `folder`.
- **Read & edit** — open a note (frontmatter + markdown) and edit it in place;
  saves are optimistically locked (a concurrent edit returns a conflict rather
  than clobbering).
- **Suche** — full unified search over the vault (the same `notes_search`
  blend: fuzzy + entity/alias + events + semantic).
- **Statistik** — top `#tags` / `@persons` by note count, notes per folder /
  OKF category, a monthly creation trend, and the most cross-linked entities.
- **Posteingang (inbox curation)** — unconsolidated fact files older than the
  Bibliothekar's stale threshold, with three actions.

## How to use it

Open `/p/notes` (Notizen). Everything is owner-scoped: you see your own notes
plus shared/household notes (default-deny).

### Curate the inbox

For each loose fact in the Posteingang you can:

- **Assign** — fold it into a topic or person note (stamps the source
  `consolidated: true`, logs to `okf/log.md`).
- **Archive** — move it under `archive/` (logged, never deleted).
- **Curate** — trigger a targeted Bibliothekar run for a scope (your own or the
  shared pool) instead of waiting for the nightly job.

This is exactly what the nightly Bibliothekar does automatically — the tab lets
you drive it by hand.

## How it works (brief)

Routes (`server.py`), all Authelia-gated and path-jailed to `NOTES_DIR`:

| route | purpose |
|---|---|
| `GET /api/portal/notes` | overview (counts, recent, librarian log); TTL-cached |
| `GET /api/portal/notes/browse?by=…` | grouped vault listing |
| `GET /api/portal/notes/note?path=…` | read one note (frontmatter + content + edit hash) |
| `PUT /api/portal/notes/note` | save (optimistic-locked by hash) |
| `GET /api/portal/notes/stats` | tag/person/folder/trend stats |
| `GET /api/portal/notes/search?q=…` | unified `notes_search` |
| `GET /api/portal/notes/inbox` | unconsolidated loose facts |
| `POST /api/portal/notes/assign` | fold a fact into a topic/person note |
| `POST /api/portal/notes/archive` | move a fact under `archive/` |
| `POST /api/portal/notes/curate` | targeted Bibliothekar run for a scope |

The page is served by the SPA shell at `/p/{type}`. Curation reuses the same
`CronRunner` machinery as the nightly Bibliothekar
(see [knowledge-system.md](knowledge-system.md)).

## Config / env

| env | purpose |
|---|---|
| `NOTES_DIR` | the vault (Syncthing-synced); OKF subtree at `notes/okf/` |
| `SOLARIS_DB_PATH` | projection read for stats / search |

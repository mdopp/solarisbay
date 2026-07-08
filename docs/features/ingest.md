# Ingest adapters

Solaris pulls your on-box data into the household knowledge graph: notes,
photos, calendar, contacts, music, email, and messenger exports. Every adapter
is **read-only on its source**, **idempotent** (re-running is a no-op when
nothing changed), and **per-resident scoped** by construction.

This doc is the operator's reference for what each adapter reads and how to
configure it. For what happens to the data afterwards (projection, curation,
retrieval) see [knowledge-system.md](knowledge-system.md); for the frozen write
shape see [`okf-write-contract.md`](../okf-write-contract.md); for the design
rationale (why exports over a messenger API, per-person IMAP) see
[`solaris-concept.md`](../solaris-concept.md) §3.5.

## What runs, and when

All adapters run inside the nightly `knowledge-night-run` cron (~02:30) via
`engine/ingest/runner.py::run_ingest`, plus an Obsidian re-ingest after the
Bibliothekar curates. An adapter with no configuration is simply skipped.

| Adapter | Source | Writes | Config (all optional) |
|---|---|---|---|
| **Obsidian** (`obsidian.py`) | hand-written vault notes | OKF concepts from `#tags`/`[[links]]` | `NOTES_DIR` (always runs) |
| **Messenger exports** (`exports.py`) | WhatsApp / Signal / SMS export drops | `person` + per-day `event` concepts | drop folder in the vault (always scanned) |
| **Immich** (`immich.py`) | photo library (EXIF geo, faces) | `event` / `place` / `person` concepts | `IMMICH_BASE_URL`, `IMMICH_API_KEY` |
| **CalDAV / CardDAV** (`caldav.py`) | calendar events + contacts | `event` / `person` concepts | `CALDAV_URL`, `CARDDAV_URL` (+ user/pass) |
| **Jellyfin music** (`jellyfin.py`) | music catalog (artists, tracks) | `song` / `band` concepts | `JELLYFIN_URL` + user/pass + owners |
| **Email — IMAP** (`imap.py`) | one curated mail folder per person | `event` concepts (kind `email`) | `IMAP_<n>_*` (numbered, per person) |

## How to use each adapter

### Obsidian & the vault

`NOTES_DIR` (default `/opt/data/notes`, Syncthing-synced) is the vault. Notes
you write by hand — plus the `fact_store` / `note_write` outputs and the OKF
subtree at `notes/okf/` — are all re-ingested. Nothing to configure beyond the
path.

### Messenger exports — drop folder

Drop an official chat export into the Syncthing-synced vault; the adapter
detects the format, parses it, writes concepts, and moves the file to
`processed/`. One adapter, per-format parsers:

- **WhatsApp** — the official "Chat exportieren" `.txt`/`.zip` (German date
  format; media omitted).
- **Signal** — signal-cli JSON / Desktop export (`"envelope"` records).
- **SMS/RCS** — JSON from an Android export app (e.g. the FOSS *SMS
  Import/Export*).

Drop location decides ownership: `notes/users/<uid>/inbox/exports/` is personal,
`notes/inbox/exports/` is household — path-based scoping, no new infrastructure.

### Immich (photos)

Set `IMMICH_BASE_URL` + `IMMICH_API_KEY` (a read-only key). Photos become
events with places (EXIF geo) and people (faces). Cross-resident *sharing* is
derived from Immich album / shared-asset membership — an Immich fact, not a
per-adapter default (write contract §6).

### CalDAV / CardDAV

Set `CALDAV_URL` / `CARDDAV_URL` and the matching `*_USERNAME` / `*_PASSWORD`.
Calendar entries become `event` concepts; contacts become `person` concepts
(feeding `@person` suggestions).

### Jellyfin music

Set `JELLYFIN_URL`, `JELLYFIN_USERNAME`, `JELLYFIN_PASSWORD`. Optional:
`JELLYFIN_CAST_URL` (LAN-reachable URL for Cast targets, defaults to
`JELLYFIN_URL`) and `JELLYFIN_LIBRARY_OWNERS` (`Name=uid;Name2=uid2` maps a
library to its owner). The catalog powers `play_music` (see
[chat-and-voice.md](chat-and-voice.md)).

### Email — IMAP (per person)

Each account is a **numbered flat env block**, and each maps to exactly one
resident, so mail is per-person scoped by construction:

```
IMAP_1_HOST=imap.example.com
IMAP_1_PORT=993              # default 993 (SSL)
IMAP_1_USERNAME=me@example.com
IMAP_1_PASSWORD=…            # env only, never logged
IMAP_1_FOLDER=Solaris       # default "Solaris" — the folder IS the filter
IMAP_1_RESIDENT=<uid>
# IMAP_2_HOST=… for the next resident, and so on
```

The **folder is the filter** (structural, not a knob): the adapter reads only
that one folder, so you curate what Solaris sees by moving mail into it. Bodies
land verbatim as `email` events; distillation is the Bibliothekar's job.

## How it works (brief)

Each adapter follows the same contract (`okf-write-contract.md` §6): pull
(read-only) → resolve/create entities (dedup via `ingest_log(source,
external_id)` + aliases) → write the OKF concept file (source of truth) →
update the `solaris.db` projection → enqueue the embedding → record
`ingest_log` (skip if `content_hash` unchanged). Incremental progress is a
cursor via `projection.get_cursor`.

<!-- screenshot: /p/notes stats after an ingest run -->

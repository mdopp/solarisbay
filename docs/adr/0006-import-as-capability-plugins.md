# ADR 0006 — Import is a generic capability; sources are plugins

**Status:** Accepted

## Context

`mdopp/solaris-import-google` is a standalone service that imports one thing (a
Google Takeout export) with a bespoke frontend and its own SSO. Folding it into
Solaris as a one-off "Google import" would just move the special-case. But the shape
— `upload → detect/classify → plan → process → write` — is generic, and future
sources (Apple, Spotify, a plain folder) and more Takeout categories share it.

## Decision

**Model import as a capability, with sources as plugins.**

- A small **`Importer` protocol** — `detect(manifest) → claims`,
  `plan(archive, selections) → ImportPlan`, `run(plan, progress) → writes` — and a
  **registry** dict (mirroring the existing `action_cards` registry).
- **Google Takeout is the first implementation.** Per category, a handler writes into
  an **existing** target, and the existing ingest projects it:
  - Calendar → Radicale (CalDAV PUT) → `DavIngest`
  - Contacts → Radicale (CardDAV PUT) → `DavIngest`
  - Keep notes → vault markdown → `ObsidianIngest`
  - YouTube-Music → album entities + facts (ADR 0003), wishlist as a query (ADR 0004)
- **The LLM does the classification** the old seed catalogs approximated
  (Hörspiel / Podcast / music / …); the cheap mechanical guards stay (text
  normalization, the `Kapitel/Folge N` pre-filter, set-cover, fuzzy ownership), and
  the rule holds: **prefer *unresolved* over *wrong*.**
- Long imports run as **durable, resumable, owner-scoped jobs** in `solaris.db`.

## How this avoids duplication

Importers write into targets that already have an ingest adapter, so the semantic
projection is **not** re-implemented per source — the Takeout importer adds almost no
new ingest code. Adding a new source = one module implementing the protocol + one
registry line.

## Consequences

- New source or Takeout category → a new `Importer` plugin, not a new service.
- Categories that are really file moves (Photos → Immich) route to the ServiceBay
  transport layer (ADR 0001) rather than a Solaris importer.

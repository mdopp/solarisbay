# ADR 0008 — Documents live in paperless-ngx; Solaris ingests projection-only

**Status:** Accepted

## Context

We built document handling *inside* Solaris: a nightly `gemma:12b` vision
extractor that OCRs each upload, classifies it into a category, and writes a
typed `document` note (extractor cron + `document_extract` tool +
`DOCUMENT_EXTRACTOR_PROMPT` + `companion_images`), rendered by DB-backed category
tables with a correction endpoint.

It works, but it re-implements a **document-management system** — badly. The 12b
pass is ~2 min/doc, it mis-reads provider names (three spellings of one
"Hellmann Worldwide Logistics"), and it hand-rolls OCR, classification,
correspondents, typed fields, a correction UI and mobile display that a
purpose-built DMS does natively and better. Meanwhile **ADR 0002 already says**
externally-sourced, re-ingestable data is *projection-only* — Jellyfin (music),
Immich (photos) and Radicale (calendar/contacts) are the precedent: the external
system owns the data, Solaris ingests knowledge from it.

Documents are the same shape. A self-hosted DMS can own them, and — decisively —
**paperless-ngx v3 speaks the local Ollama directly** (embeddings via `sqlite-vec`
+ LLM inference for metadata suggestions), so LLM extraction stays on-box with no
cloud and no unmaintained add-on.

## Decision

**paperless-ngx (target v3) is the document backend; Solaris ingests
projection-only, like Jellyfin/Immich/Radicale.**

- **paperless owns** OCR, storage, classification (its correction-learning
  classifier + optional local-Ollama LLM suggestions), correspondents, typed
  custom fields, full-text search, and the web + mobile UI including
  human-in-the-loop correction. Uploads land in paperless, not Solaris.
- **Solaris owns knowledge.** A new `PaperlessIngest` adapter (ADR 0006 shape,
  sibling of `DavIngest`) reads paperless via its REST API and projects each
  document into the OKF substrate:
  - paperless *document* → OKF `document` entity + typed facts (source
    `paperless`, ADR 0003 — so a human correction still wins and coexists).
  - paperless *correspondent* → OKF `organization` **or** `person` (Solaris makes
    the split paperless doesn't — see below), linked from the document.
  - paperless *custom fields* / dates → OKF facts (`policy_number`,
    `cancellation_deadline`, …) driving the existing calendar + contact syncs.
- **Push, not poll.** A paperless **Workflow webhook** (consume/updated) hits a
  Solaris endpoint that enqueues the one document's re-ingest; a periodic API
  sweep is the reconcile/backfill fallback. This replaces the nightly extractor.
- **Retire the in-Solaris extractor:** the 12b cron, `document_extract`,
  `DOCUMENT_EXTRACTOR_PROMPT`, `companion_images`/vision, and the upload→OCR path.
- **The org/person split lives in OKF, not the DMS.** paperless has a single flat
  correspondent; Solaris already has `organization` + `person` types and resolves
  which at ingest — the richer modeling stays where the knowledge graph is.

## How this avoids duplication

paperless holds each document once (its DB + files); Solaris holds only the OKF
projection, rebuildable by re-ingesting paperless — exactly ADR 0002. The whole
**back half we already built is unchanged** — the contacts graph, provider dedup,
deadline→calendar and CardDAV syncs, coverage lookup — because it operates on OKF
entities regardless of source. Only the extraction **front** is swapped: an
adapter replaces the extractor, adding almost no new *ingest* code (ADR 0006).

## Consequences

- A new ServiceBay service (paperless-ngx v3 + Postgres + Redis) — a template in
  this repo, a dependency like Jellyfin/Immich. LLM suggestions point at the box
  Ollama; nothing leaves the box.
- Correspondent **merge in paperless's UI** becomes the primary provider-dedup
  (fixes the Hellmann OCR-variance the normalization can't); Solaris's normalized
  `provider_key` stays as a secondary grouping key.
- The Notizen "Dokumente" tables become a **read** view over the OKF projection;
  editing/correction moves to paperless (its UI + mobile are the win).
- Existing uploads (the 18 test docs) migrate into paperless, then re-project.
- **Beta risk:** v3 is late-beta. PoC on v3-beta to validate native-Ollama
  extraction + correspondents/custom-fields against real docs before migrating;
  production either waits for v3 stable or starts on v2.20-stable and upgrades
  (Whoosh→Tantivy reindex is a supported migration).

## Rollout (slices)

1. **PoC** — v3-beta via ServiceBay; run the 18 docs through it; confirm
   local-Ollama suggestions, correspondents and custom fields carry what the OKF
   adapter needs (provider, policy number, dates, category).
2. **Template** — `templates/paperless/` (paperless-ngx + Postgres + Redis),
   Ollama wired to the box; a `solaris` API token minted for the adapter.
3. **`PaperlessIngest` adapter** — REST read → OKF `document`/`organization`/
   `person` + facts (`source=paperless`); the webhook receiver + a reconcile
   sweep. The contact/deadline syncs run unchanged off the new projection.
4. **Cut over** — route uploads to paperless; retire the extractor cron + tool +
   vision path; migrate the existing docs; keep the category tables as a read view.
5. **Refine** — correspondent-merge workflow for dedup; map paperless document
   types → OKF categories; fold document links into the "Vorgang" grouping.

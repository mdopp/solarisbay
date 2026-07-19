# ADR 0002 — Provenance decides the substrate

**Status:** Accepted

## Context

The OKF model's guiding rule is *"markdown files are the source of truth, the
SQLite projection (`entities`/`facts`/`events`/`concepts`/`okf_vectors`) is a
rebuildable index."* That is correct for knowledge that **originates with the user**
— there the markdown note is the only place the fact exists.

But a large amount of what we ingest is **externally sourced and re-ingestable**:
the Jellyfin music library, Immich photos, CalDAV/CardDAV. For those, the *external
system* is the real source of truth. Today Jellyfin ingest writes a per-song **and**
per-artist OKF markdown file (thousands of near-empty files) plus a per-concept
embedding — so the same library is effectively held three times (Jellyfin's DB, the
markdown, the projection). That is the duplication this ADR removes.

## Decision

**Provenance decides where a concept's source of truth lives.**

- **Externally re-ingestable** (Jellyfin, Immich, Radicale) → **SQLite projection
  only** (rebuildable by re-ingesting the source). **No** per-item markdown.
- **Self-originated** (the physical-collection note with its sleeve photo, personal
  facts, "used to love", Keep notes) → **markdown note = source of truth**
  (+ projection + embedding).

## How this avoids duplication

Markdown-as-truth is used **only where the note is the single source**. Data whose
truth lives in Jellyfin/Immich is never copied into markdown — it is projected into
SQLite and can be dropped and rebuilt by re-ingesting the source.

## Consequences

- Jellyfin ingest stops materializing per-song markdown; songs become facts/rows and
  the RAG-worthy nodes are album/artist (see ADR 0003, ADR 0005).
- Existing per-song markdown is pruned on the substrate rework (#873).
- A concept can carry facts from *both* provenances (a Jellyfin `has_digital` fact
  and a note-sourced `owned_physical` fact) without being two objects — see ADR 0003.

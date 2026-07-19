# ADR 0005 — Lean RAG granularity; no album cover art

**Status:** Accepted

## Context

Every OKF concept currently gets a whole-concept embedding (`okf_vectors`). With
per-song concepts that is thousands of near-identical song embeddings — noise for
semantic search, little value. Separately, "album cover" imagery is tempting to fetch
and store, but it is externally-owned, heavy, and re-fetchable — exactly the kind of
duplication ADR 0002 warns against.

## Decision

- **Embed at meaningful granularity.** Embeddings live on **album / artist** entities
  and on **personal notes** (physical-collection notes, "used to love" narratives) —
  **not** per song. The RAG index carries the nodes worth searching semantically.
- **No album cover art materialized.** We do not fetch or store digital cover images.
  The only image kept is the user's **own sleeve photo** on the physical-collection
  note (which is genuinely self-originated data — ADR 0002).

## How this avoids duplication

Cover art lives at the source (Jellyfin/streaming) and is re-fetchable, so we do not
copy it. Embeddings are not duplicated per song; the album/artist node carries the
one embedding worth keeping.

## Consequences

- The Jellyfin ingest rework stops per-song embedding; album/artist nodes are the
  RAG surface for the library.
- A "show me the cover" need is served live from the source or from the user's own
  sleeve photo, not from a stored copy.

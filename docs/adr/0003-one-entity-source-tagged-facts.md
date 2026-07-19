# ADR 0003 — One entity per real-world thing; sources contribute source-tagged facts

**Status:** Accepted · satisfies requirement **B3**

## Context

Today there is no `album` entity. Jellyfin writes `band` (artist) and `song`
concepts; the album is only a **string** in a song's frontmatter (`album:`), never
projected. So "what do I own / want / used to love" about an album has no single
node to attach to — it lives smeared across song files, a separate wishlist note,
and Jellyfin. The physical collection (records/LPs/cassettes you own in the real
world, want to digitize, used to love) has nowhere coherent to go.

## Decision

**One thing in the real world = one entity. Each source contributes only
`source`-tagged facts to it.**

```
artist ──has──▶ album ──contains──▶ song
                  │
                  ├── has_digital            (source = jellyfin)
                  ├── owned_physical: vinyl  (source = note + sleeve photo)
                  ├── wishlist               (source = import)
                  ├── used_to_love           (source = stenograph / chats)
                  └── source: "Freund X"     (source = note)
```

`album` and `artist` become first-class OKF entities. Every fact carries `source`
(and confidence) so its origin is known.

## Value (why B3 is worth it)

This is **not new machinery** — it reuses the OKF `entities`/`facts` model that
people/events/places already use. The value of applying it to music:

1. **Cross-source questions at one node.** *"Which albums did I used to love but
   don't own digitally?"* = a query over `used_to_love` & `!has_digital`. Without one
   node you would join Jellyfin + a note + chat logs by hand.
2. **Provenance & trust.** Every fact knows its `source`, so it can be weighted,
   expired, and audited — a chat-inferred `used_to_love` is softer than a Jellyfin
   `has_digital`. A household assistant needs this.
3. **Dedup by construction.** The import tool's real pain was fuzzy-matching albums
   across *separate* lists (tag typos, compilations, "The " prefixes). One node +
   alias resolution removes that whole class of bug.
4. **Music joins the knowledge graph.** It becomes queryable/relational (RAG,
   reasoning) instead of a siloed list — the same first-class treatment as people
   and places.
5. **Extensible for free.** A new fact type (`lent_to`, `signed_copy`,
   `condition: mint`) needs zero new plumbing.

**When it would be over-engineering:** if music were only a flat "own it / don't"
checkbox, a plain table would do. B3 pays off precisely because the goal is to
*reason* over music (nostalgia, gifts, digitize priorities) — the "know what I used
to love" goal is only answerable if music is in the graph.

## How this avoids duplication

Physical, digital, and wishlist claims about one album **converge on one node** via a
fact-join, replacing fragile string-matching across parallel lists.

## Consequences

- Introduce `album`/`artist` entities; Jellyfin attaches `song`s and a `has_digital`
  fact instead of writing per-song markdown (ADR 0002).
- The `#859` enrichment is reworked to write `owned_physical`/`wishlist`/`source` as
  **facts on the album entity**, not into a markdown wishlist note.
- Derived lists become queries over these facts — see ADR 0004.

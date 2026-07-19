# ADR 0004 — Derived lists are queries, not stored artifacts

**Status:** Accepted

## Context

The standalone import tool materialized a `type: music-wishlist` markdown note — a
concrete list of albums to buy. Any materialized list immediately drifts: the moment
you rip an album or buy it, the list is stale until something rewrites it. With one
album entity carrying `source`-tagged facts (ADR 0003), the "list" is fully implied
by the facts.

## Decision

**Lists that are derivable from facts are computed as queries, not stored.**

- **Wishlist / shopping list** = `played & !has_digital & !owned_physical`.
- **"Just rip it"** = `owned_physical & !has_digital`.
- **"What did I used to love"** = `used_to_love` (optionally `& !has_digital`).

These are views over the album entities and their facts, rendered on demand
(in chat, on a page, or as an exported note *snapshot* when the user asks) — the
store stays the fact graph.

## How this avoids duplication

There is no second copy of the list to keep in sync with reality. The facts are the
single source; the list is a projection of them at read time.

## Consequences

- The nightly Bibliothekar keeps the *facts* current (e.g. reconciling
  `owned_physical` against `has_digital`); it does not maintain a list file.
- A user-visible wishlist note, if wanted, is an **exported rendering** of the query,
  explicitly a snapshot — not the source of truth.

# ADR 0010 — One person entity; every people-surface reads it

**Status:** Accepted

## Context

"A person" is represented in Solaris in two unconnected substrates that never
meet, so there is no single source of truth for a contact:

1. **`person` entities** (OKF, `okf/people/…`), written by several ingest/create
   paths, each with its own identity scheme and **no cross-source or
   cross-resident de-duplication** — because `writer.write_concept` skips the
   `resolve_entity` name/alias match whenever an `identity_key`/`external_id` is
   set. So the same human accretes a separate entity per source:
   - `.contacts` create — `source="contact"`, `identity_key="contact:{uid}:{random}"` (a fresh UUID **every** create → not even self-dedup).
   - CardDAV ingest — `source="carddav"`, `external_id=<vcard uid>`.
   - Immich faces — `source="immich"`, `external_id="person/{immich_face_id}"` (per-library → duplicates across residents).
   - Chat exports (WhatsApp…) — `source="exports"`, `external_id="{file}:person:{slug}"` (per export file).
   - Google-Takeout contacts — via CardDAV PUT → the `carddav` path.
2. **Chat mentions** (`mentions_store`, plain name strings) — which back the
   **"Personen" doorway** on the Notizen page **and** the **`@`-mention
   autocomplete/resolution**. These surfaces **never read the person entities**.

Consequences today: the "Personen" doorway, the `.contacts` list (person entities
*with an email/phone fact*), and `@`-autocomplete each show a **different set**;
one person exists many times; `@mike` does not resolve to the `Michael` contact.
Separately, the UI label **"Kontakte"** means two different things — the document
**providers** (`type='organization'`) *and* the personal `.contacts` persons —
and CardDAV is read-in for persons but write-back only for organizations.

This violates [ADR 0003](0003-one-entity-source-tagged-facts.md): albums converge
onto one entity with source-tagged facts; persons do not.

## Decision

**One `person` entity per human is the single source of truth, and every
people-surface reads it.**

1. **One canonical person entity.** A person has a stable canonical
   `identity_key` (a normalized-name slug, resident/household-scoped), and every
   write path (`.contacts`, CardDAV, Immich, exports, Takeout) **converges** on it
   via `resolve_entity` name/alias matching — even when the source also carries an
   `external_id`. Sources contribute **source-tagged facts** (email/phone from
   `.contacts` + CardDAV, a face link from Immich, a mention count from chat),
   never a parallel entity. `external_id` becomes a *fact/alias* on the one
   entity, not a second identity. (ADR 0003, applied to persons.)
2. **Every surface reads the person entities.** The "Personen" doorway, the
   `.contacts` list, and `@`-mention autocomplete **and** resolution all query the
   same `person` entities. Chat `@`-mentions become an **`alias`/mention fact** on
   the entity, not a separate `mentions_store` universe — so `@mike` → the
   `Michael` entity, and every surface agrees on the count. (Folds in #968.)
3. **Aliases are facts.** `@mike`→`Michael`, `Oma`→`Erika Musterfrau` live as
   `alias` facts on the person entity; resolution and autocomplete use them.
4. **Person ≠ organization, and the UI says so.** `organization` (document
   providers) stays a distinct type; its surface is renamed away from "Kontakte"
   (e.g. **"Anbieter"**) so "Kontakte/Personen" unambiguously means humans.
5. **CardDAV is a projection of the person entities, both ways.** Personal
   `.contacts` persons sync **out** to the resident's address book (not just
   organizations), and the address book syncs **in**, keyed by the same canonical
   identity so a round-trip does not fork the entity.

Merging humans is destructive if wrong, so this ships as **verifiable slices**
(surface-unification first, then cross-source dedup/merge, then CardDAV
write-back), each box-verified — not a big-bang. The slices are tracked as issues;
this ADR pins the target.

## Consequences

- One mental model: "a person is one entity; `.contacts`, the Personen doorway,
  `@`-mentions and CardDAV are all views of it." The 3-vs-2 discrepancies vanish.
- `@`-mentions gain real identities (aliases, dedup) instead of free-text names.
- Cross-source/cross-resident dedup needs a **safe merge** (confidence-gated,
  reversible) — a wrong merge conflates two people, so it is gated and sliced.
- Immich/exports stop minting duplicate people; their signal attaches to the
  canonical entity as facts.
- Naming split (`Anbieter` vs `Kontakte`) is a small UI change that removes a
  standing confusion.

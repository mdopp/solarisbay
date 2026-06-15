# OKF Write Contract — Solaris household knowledge

> **Status:** frozen 2026-06-15 (see §9). The prerequisite for building the
> ingestion adapters (#204/#206/#207). Defines the **write side only** (CQRS
> write). `gbrain` (read/retrieval) consumes this later and is out of scope here.

## 0. Why this exists

Ingestion (photo / voice / calendar / contacts / media → knowledge) is the
**write path** and is independent of `gbrain` (the read path). But every adapter
must write into the *same* shape, or we re-ingest later. This contract fixes that
shape: how an ingested signal becomes (a) an **OKF concept file**, (b) a
**rebuildable structured index** in `solaris.db`, and (c) an **embedding**.

## 1. Principles

- **OKF files = source of truth** for knowledge. `solaris.db` knowledge tables =
  a **rebuildable projection/index** of them. Embeddings = derived. → you can
  `git clone` the vault and rebuild the index + vectors from scratch.
- **Operational state** (sessions, timers, settings, speaker-ID) — `.db` *is*
  the source of truth; it is **not** knowledge and not in OKF.
- **Layer by data type, no central store.** No greenfield monolith.
- **Per-resident scoping (`uid`) and provenance on everything.**
- **Idempotent ingestion** — re-running an adapter is a no-op when nothing
  changed (`ingest_log` + `content_hash`).
- **On-box only.** Adapters read their source locally; no cloud write path.

## 2. OKF bundle layout

A **dedicated OKF subtree** under the existing Obsidian vault (kept separate from
hand-written notes): `notes/okf/`. Domain-tree of markdown:

```
notes/okf/
  people/<slug>.md
  events/<YYYY-MM-DD>-<slug>.md
  places/<slug>.md
  books/<slug>.md
  songs/<slug>.md   bands/<slug>.md
  trips/<slug>.md
  index.md   (per dir, optional)
  log.md     (append-only change history, optional)
```

- One concept = one `.md`. Slug = lowercase, digits, dashes (the OKF safe-slug
  rule — no `/`, `..`, leading dots, whitespace).
- Events are date-prefixed for natural temporal sort.

## 3. Concept frontmatter (OKF-conformant)

**Common (all types):**

| key | req | meaning |
|---|---|---|
| `type` | ✅ | OKF concept type: `person`/`event`/`place`/`book`/`song`/`band`/`trip` |
| `id` | ✅ | stable id; **equals** the `.db` `entities.id`/`events.id` |
| `title` | ✓ | display name |
| `description` | ✓ | one-line summary |
| `resident` | ✅ | owning resident `uid`, or `household` (shared) |
| `source` | ✅ | provenance: `<adapter>:<external_id>` (e.g. `immich:asset/abc`) |
| `timestamp` | ✓ | ISO-8601 last-modified |
| `resource` | – | canonical URI of the underlying asset (Immich/abs/Jellyfin/contacts) |
| `tags` | – | cross-cutting labels |

**Per type (additional):**
- `person`: `aliases[]`, `contact` (contacts URI); relationships via body links.
- `event`: `when` (ISO datetime or range), `where` (→ `places/…` link),
  `participants` (→ `people/…` links), `kind`, `media[]` (Immich URIs).
- `place`: `geo` (lat,lon), `address`.
- `book`: `author`, `status` (`read`/`reading`/`want`), `rating`, `resource` (abs).
- `song`/`band`: `artist`/`genre`, `rating`, `resource` (Jellyfin).
- `trip`: `dates`, `destinations` (→ `places/…`), `participants` (→ `people/…`).

**Relationships (light convention — standard for our writers):** a
`## Relationships` section with `- <rel> → [[<path>]]` lines, e.g.
`- saw → [[people/anna]]`, `- at → [[places/club-x]]`. OKF-conformant: a
consumer ignoring the `<rel>` prose still sees a plain directed link. Writers
emit this section; the `<rel>` verb projects to `event_entities.role` /
`facts.predicate` in the `.db` index.

## 4. `solaris.db` — knowledge index (NEW tables, rebuildable projection)

Alembic migration in `database/`, **alongside** the existing operational tables.
All rows carry `resident_uid` (or `household`). Rebuildable from OKF.

```
entities(        id PK, type, canonical_name, resident_uid, source, content_hash, updated )
entity_aliases(  entity_id FK, alias )
facts(           id PK, subject_entity_id|resident_uid, predicate, value,
                 confidence, source, timestamp )            -- queryable prefs/relations/attrs
events(          id PK, ts, resident_uid, kind, source )
event_entities(  event_id FK, entity_id FK, role )          -- (saw|with|at|about)
concepts(        id PK, ref_id (entity|event), okf_path, embedding_id,
                 content_hash, updated )                      -- the cross-layer link table
ingest_log(      source, external_id, content_hash, ingested_at )  -- PK (source, external_id)
```

Indices: `events(ts)`, `events(resident_uid, ts)`, `facts(subject, predicate)`,
`entity_aliases(alias)`, `ingest_log(source, external_id)`.

This index makes “who did I see last week” a `events` range+join and
“recommend a book” a `facts` filter — without grepping the vault. Content/narrative
stays in the OKF file; the `.db` row is pointer + filter fields + `content_hash`.

## 5. Embedding policy

- **Embed:** the concept body text (title + description + body), one vector per
  concept; chunk only if long. Model: `nomic-embed-text` (existing). Stored in
  the holographic/episodic store, keyed by `concepts.embedding_id`.
- **When:** on create/update where `content_hash` changed (re-embed on change).
- **Don’t embed:** pure structured facts (queried via `.db`), raw media.

## 6. Ingestion adapter contract

Each adapter (Immich · calendar · contacts · Obsidian · media) is a **writer**:

1. pull from its source (**read-only** on the source);
2. resolve/create entities (dedup by `ingest_log(source, external_id)` + alias
   resolution);
3. write/update the **OKF concept** (source of truth);
4. update the **`.db` projection** (entities/facts/events/concepts);
5. enqueue the **embedding**;
6. record **`ingest_log`** (idempotent upsert; skip if `content_hash` unchanged).

Adapters are independent + parallel, **never read gbrain**, and tag every
concept/fact with the owning `resident_uid`. **Default scope = the uploading /
ingesting resident.** Cross-resident *sharing* is not modelled here — it is
derived from **Immich** (album / shared-asset membership): the Immich adapter
maps a shared asset to the residents Immich shares it with. So "shared" is an
Immich fact, not a per-writer default.

## 7. Provenance · dedup · deletion

- Re-ingest = upsert by `(source, external_id)`; unchanged `content_hash` → skip.
- **Deletion / GDPR:** removing a resident or a source cascades → OKF files +
  `.db` rows + embeddings for that scope. Per-resident scoping keeps it clean.

## 8. This is NOT

- Not `gbrain` (read/retrieval/ranking).
- Not the operational `.db` schema (sessions/timers/settings/speaker-ID).
- Not media storage (Immich/filesystem own bytes; we reference by URI).

## 9. Decisions (frozen 2026-06-15)

1. **Vault location:** dedicated `notes/okf/` subtree (separate from hand-written
   notes). [§2]
2. **Relationships:** light `## Relationships` `rel → [[…]]` convention, standard
   for writers. [§3]
3. **Default scope:** the uploading/ingesting resident; cross-resident sharing is
   derived from Immich (album/shared membership), not a writer default. [§6]
4. **Embedding granularity:** whole concept = one vector. [§5]
5. **Facts source of truth:** authored in OKF, projected to the `facts` table. [§4/§5]
6. **Adapters in scope:** **all four** — Immich, calendar, contacts, Obsidian.
   Build order: foundation (`.db` migration → OKF-writer/entity-resolver +
   embedding) first, then Immich (richest: entities + events + faces) → calendar
   → contacts → Obsidian.

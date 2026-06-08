# Solilos architecture

Canonical reference for the Solilos household AI assistant. For the
deployment layout (templates, images, install paths) see
[`README.md`](README.md).

---

## 1. Inference engine

Ollama on the box (RTX 2000 Ada 16 GB).

| Model | Role |
|---|---|
| `gemma4:12b` | Default / thorough reasoning |
| `gemma4:e2b` | Fast / tool-heavy turns |

**Adaptive model routing** — every session carries a *speed hint*:

- **Schnell** (fast) → `e2b`: ~4× faster prefill, reliable HA tool-calls, voice-latency budget.
- **Gründlich** (thorough) → `12b`: complex reasoning, longer context synthesis.

Context window: 131 072 tokens (`OLLAMA_CONTEXT_LENGTH=131072`).

Speculative decoding / MTP is not attainable on the current CUDA/GGUF stack;
that decision is final (see repo history / #189).

No embeddings are wired yet. When semantic search is added it will target a
dedicated `nomic-embed-text` instance on a separate Ollama runner so it does
not compete for the generation slot.

---

## 2. Knowledge architecture (4 layers, CQRS)

Reads go to the right layer; writes and actions flow via MCP/API (CQRS).

| Layer | Store | Status |
|---|---|---|
| **L1 — episodic / user facts** | Hermes-native `holographic` provider | active |
| **L2 — freeform text** | Obsidian notes vault (`/opt/data/notes`, Syncthing) + `notes-search` skill; `qmd` semantic upgrade optional | active |
| **L3 — structured knowledge** | `solilos.db` (SQLite) — today: `system_settings`, `cloud_audit`, `voice_embeddings`; Phase-3a domain collections + entity/interaction graph **deferred** to gbrain v0.43+ (gbrain's typed self-wiring did not work in v0.42) | partial |
| **L4 — live device state** | HA-native `homeassistant` toolset | active |

The `solilos.db` schema is managed by Alembic migrations in `database/`
(hand-rolled SQL via `op.execute`; portable to Postgres if Phase 3a calls for it).
See [`database/README.md`](database/README.md) for the migration runbook.

---

## 3. Topics / Contexts

A **Topic** is a cross-cutting, persistent label that groups a theme, project,
or context across chats, notes, and future graph nodes.

### Built-in topics

| Topic slug | Type | Model | Persona |
|---|---|---|---|
| `household` | system | e2b | household soul |
| `servicebay-admin` | system | 12b | admin soul |

### User topics (examples)

`finanzen`, `daggerheart`, `krankenkasse`, `arbeit`,
`projekt/wintergarten`, `projekt/garagenumbau`, …

### Operator decisions

**D1 — one primary topic per chat.**
A chat has exactly one *primary* topic and may carry any number of *secondary*
tags. This keeps routing and persona assignment deterministic.

**D2 — a topic carries a primary model + persona.**
Assigning a topic to a chat sets the chat's default model and persona (e.g.
`household` → e2b + household soul; a project topic → operator-chosen
model/persona). This is the mechanism that ties adaptive model routing (#187)
to the pinned-persona pattern (#229/#237).

**D3 — scope default is per-resident.**
Per-resident isolation is the baseline (#153). A topic can be widened to
*shared* (household, accessible to all residents) or *admin*.

**D4 — topic creation is suggested AND manual.**
Sol detects a recurring theme mid-conversation and asks "Soll ich das als
eigenes Topic anlegen?" Manual creation is always available.

### Schema

**`topics` table** (registry, in `solilos.db`):

| Column | Type | Notes |
|---|---|---|
| `slug` | TEXT PK | e.g. `projekt/wintergarten` |
| `display_name` | TEXT | Human label |
| `parent` | TEXT FK→topics | Hierarchy: `projekt/wintergarten` → parent `projekt` |
| `scope` | TEXT | `resident` / `shared` / `admin` |
| `owner_uid` | TEXT | LLDAP uid; null for system topics |
| `default_model` | TEXT | `e2b` / `12b` / null (inherits) |
| `default_persona` | TEXT | Soul slug or null |
| `color` | TEXT | Hex accent for the UI chip |
| `archived` | INTEGER | 0/1 |

**`session_topics` table** (chat↔topic assignment):

| Column | Type | Notes |
|---|---|---|
| `session_id` | TEXT FK | Hermes session id |
| `topic_slug` | TEXT FK→topics | |
| `role` | TEXT | `primary` / `secondary` |
| `owner_uid` | TEXT | Resident who assigned it |

### UI surfaces

- **Topic picker** in the chat header (alongside Schnell/Gründlich and
  persona selector).
- **Topic chip** in the session list (visual at-a-glance).
- **Pinned topic-chats** in the rail — pre-assigned topic + model/persona
  (the #237 pattern extended to user topics).

### Data → topic tagging (the heart of the system)

Every ingestion from a topic-T chat auto-stamps `#topic/<slug>`:

| Ingestion path | Tag mechanism |
|---|---|
| Notes (media-ingestion-multimodal, dynamic-skills facts, daily-chronicle) | Frontmatter `#tags:` — these already write `#tags`; the active topic tag is appended |
| Future Immich photos | Topic album |
| Holographic facts (L1) | Topic metadata field |
| Future `solilos.db` L3 records | `topic` column / graph edge |

Mechanism: the proxy injects the active topic slug into each turn's system
context. Any ingestion skill that runs during that turn reads it and stamps
`#topic/<slug>`.

### Retrieval

- `notes-search` filtered by `#topic/<slug>` (works today once tagging lands).
- **Topic dashboard**: all notes, images, facts, and events for a topic in one
  view.
- Future: graph query by topic label (gbrain v0.43+).

---

## 4. Temporary / Incognito chats

Ephemeral by default: no durable session persistence, no auto-ingestion, no
memory/learning writes, no compaction. The session is deleted on close — like
browser incognito.

**Retroactive selective persistence** — the escape hatch: mid-conversation the
resident can say "Erstelle hieraus eine Notiz im Topic Finanzen." The proxy
reads the live context, writes exactly that note (tagged with the chosen topic)
via the normal ingestion path, and leaves everything else ephemeral.

| Property | Ephemeral session | Normal session |
|---|---|---|
| Compaction | skipped | runs at ~90–95% context |
| Auto-ingestion | skipped | active |
| Memory/learning writes | skipped | active |
| Explicit "extract to note" | available | n/a |
| Session on close | deleted | persisted |

Mechanism: an ephemeral flag on the session (alongside the persona/topic
markers); the proxy checks it before every write path.

---

## 5. Phasing

**v1 (no gbrain dependency):**
Topics registry + `session_topics` + topic picker + per-topic model/persona +
auto-`#topic/` tagging + topic-filtered notes-search + topic suggestion +
temporary/incognito chats.

**v2 (gbrain v0.43+):**
Topics become first-class graph nodes/labels. Chat→topic and data→topic
assignments become typed edges. Cross-source topic retrieval runs over the
graph. The v1 `#topic/<slug>` tags map 1:1 to graph labels — forward-compatible,
no migration of tagged notes required.

---

## 6. Cross-cutting constraints

- **Per-resident isolation** (#153) — session ownership, topic scope, and data
  writes are all resident-scoped by default.
- **Pinned-persona / marker pattern** (#229/#237) — topic assignment reuses the
  same session-marker mechanism as persona pinning.
- **Notes `#tag` mechanism** — already used by media-ingestion, dynamic-skills,
  and daily-chronicle; topic tagging extends it without a new convention.
- **Minimal knobs** — one global/automatic mechanism per concern, not per-feature
  toggles. Topic routing and ephemeral flags follow this principle.

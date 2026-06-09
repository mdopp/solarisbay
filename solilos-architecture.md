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

**Model per profile, speed per turn** — post-#293 (§2) the **profile** pins the
base model (household → `e2b`, admin → `12b`); the per-turn speed hint maps to
`reasoning_effort`, not a per-session model swap:

- **fast** (Schnell) → `reasoning_effort: none`: skips the thinking block (#222),
  voice-latency budget, reliable HA tool-calls.
- **thinking** (Gründlich) → `reasoning_effort: high`: surfaces a reasoning block
  for complex turns.

(Pre-#293 the speed hint swapped the model per session; that override is retired
— the model is now profile-owned, the effort is the per-turn lever.)

Context window: 131 072 tokens (`OLLAMA_CONTEXT_LENGTH=131072`).

Speculative decoding / MTP is not attainable on the current CUDA/GGUF stack;
that decision is final (see repo history / #189).

No embeddings are wired yet. When semantic search is added it will target a
dedicated `nomic-embed-text` instance on a separate Ollama runner so it does
not compete for the generation slot.

---

## 2. Multi-profile Hermes (#293)

Each persona is a **Hermes profile**, and Solilos runs **one gateway instance
per profile** — both inside the **single `hermes` container** of the `solilos`
ServiceBay service. A lean profile is the perf lever (benchmark #291/#292):
~1.9–4k prompt + ~2–3s warm turns vs the default profile's ~28–30k / 25–80s.

### One container, two profile gateways (the s6-safe mechanism)

The hermes image runs an **s6-overlay** entrypoint. The container's CMD must be
`args: [gateway, run]` (NO `command:` override, NO `-p` in the CMD): overriding
`command:` with `hermes -p <profile> gateway run` bypasses the entrypoint, s6
tries to exec `-p`, and the container crash-loops — that is the **#294 revert**
(it took the live assistant down 2026-06-09). The validated mechanism instead
runs **two gateways in the one container**:

- `household` is set as the **sticky default** (`hermes profile use household`),
  so the container's bare `gateway run` serves it on `HERMES_API_PORT` (8642).
- the `admin` gateway is **started alongside** it with
  `hermes -p admin gateway start` (the `start` subcommand, fired from the
  post-deploy after the restart). The image's
  `/etc/cont-init.d/02-reconcile-profiles` init manages that gateway's own s6
  slot, so it binds `HERMES_ADMIN_API_PORT` (8643) beside the household gateway
  in the **same** container — box-confirmed s6-safe (2026-06-09).

| Profile | Gateway | Port | Model | Soul / skills / tools |
|---|---|---|---|---|
| `household` (Sol) | container default (`gateway run`, sticky-default profile) | 8642 | `gemma4:e2b` | resident soul, `.no-bundled-skills` + the ~5 household skills, holographic memory, HA toolset, `servicebay-mcp` + `gatekeeper-mcp` — **no admin MCP** |
| `admin` (operator) | started in the same container (`hermes -p admin gateway start`) | 8643 | `gemma4:12b` | operator soul, `sol-admin-*` skills, `servicebay_admin` + `servicebay-mcp`, admin-gated |

The `solilos` post-deploy **provisions each profile** (`hermes profile create`,
then writes its `config.yaml` — model / `providers.ollama` / its own `api_server`
port / toolsets / MCP / SOUL + drops `.no-bundled-skills`) instead of one global
`config.yaml`. Each profile's config pins its **own** `api_server` port, so the
two gateways bind distinct host ports without a race. This structurally fixes the
#268 `servicebay_admin` leak (admin MCP lives only in the admin profile's config)
and the #291 skills bloat (household loads ~5 skills, not 105).

Each named profile is a separately-locked Hermes gateway: a named profile gets
its own `/opt/data/profiles/<name>/gateway.lock` + `logs/gateways/<name>/lock`,
so the two gateways coexist in the one container without the #271 `default`-lock
deadlock (which was two *containers* fighting the same `default` lock).

### Shared data vs isolated profile state

Both gateways run in the one container, mounting the **same** volumes, so they
run "auf den gleichen Daten":

| Data | Shared across both gateways? | Where |
|---|---|---|
| `solilos.db` (rooms / voice / domain, L3) | **shared** | `/var/lib/solilos` (`solilos-data`) |
| Notes vault (L2) | **shared** | `/opt/data/notes` |
| Chat attachments | **shared** | attachments volume |
| HA + Ollama access | **shared** | container env (`HASS_URL`/`HASS_TOKEN`, Ollama) |
| Hermes per-profile **memory** (holographic, L1), **skills**, soul, config | **isolated per profile** | `/opt/data/profiles/<name>/` (under the shared `hermes-data` volume) |

So `solilos.db` + notes are read/written by **both** profiles, but each profile's
holographic memory, skills, and soul are its own. **Holographic memory is
household-scoped**: the household profile's `/opt/data/profiles/household/memory`
is **not** shared with admin (the operator decision — admin must not see
household facts), and vice-versa.

### Routing (chat + voice → the right instance)

- **solilos-chat proxy** picks the gateway per session/persona/admin-gate:
  household chat + pinned Zuhause + every resident session → the household
  gateway (`HERMES_URL`, 8642); the `?persona=servicebay-maintenance` admin
  embed (#209) → the admin gateway (`HERMES_ADMIN_URL`, 8643), admin-gated. A
  session is pinned to the gateway it was created on (Hermes session state is
  per-gateway), and the #209/#229 admin gate holds at the router — a non-admin
  is always routed to household, even presenting a known admin `session_id`.
  Falls back to household when no admin gateway is configured.
- **gatekeeper (voice)** → always the household gateway (residents speak to Sol,
  never the admin profile); it carries no admin URL / admin port.
- The #278 persona×speed dropdown selects a **profile**: Sol-fast / Sol-thinking
  → household profile (the speed maps to per-turn reasoning_effort, below);
  Admin → admin profile.

### What the profile subsumes (#293 finalization)

The household gateway's profile now **owns the soul + the base model**, so the
chat proxy no longer injects a per-session **persona overlay** (the
`personalities.py` `system_prompt` injection at create) or a per-session **model
override** — those would fight the profile. The `personalities.py` catalog is
kept for the dropdown labels; only the redundant *injection* is dropped. A
session is created with an empty `system_prompt`/`model`, which lets the profile
supply both.

What the profile does **not** pin stays in the proxy, per turn / per session:

- **speed → `reasoning_effort`** (#222/#278): "fast"/"thinking" → `none`/`high`,
  chosen per turn (the profile pins the model, not the per-turn effort).
- **topic binding** (#241/#242): a chat under a topic is still persisted as its
  primary assignment + gets the `#topic/<slug>` context hint (the topic no
  longer overrides model/persona — the profile owns those).
- **pinned Zuhause** (#237), **`[Aktuelle Zeit]` per-turn injection** (#265),
  **incognito `[temp:]` prefix + guard** (#246).

---

## 3. Knowledge architecture (4 layers, CQRS)

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

## 4. Topics / Contexts

A **Topic** is a cross-cutting, persistent label that groups a theme, project,
or context across chats, notes, and future graph nodes.

> **Pivot (#279) — user-facing tagging is mention-based, not a picker.**
> The structured **Thema topic-picker** built in #241/#242 is *retired* as the
> user-facing entry point: the topic list couldn't be user-edited and residents
> don't want to curate a fixed list. It is replaced by inline **`#tag`**
> (tags) and **`@person`** (persons) mentions typed directly in the chat. The
> **system topic *binding* stays internal** — the Zuhause chat still runs on
> `gemma4:e2b` + the household soul (now via the household **profile**, §2,
> not a per-session topic override), the `topics` table and its
> `household` / `servicebay-admin` system rows remain as internal plumbing. Only
> the *user-facing picker* is replaced. The split is explicit: **internal
> binding** (D2, unchanged) vs **user-facing tagging** (mentions, below). The
> picker-era design that follows is kept as history and marked
> **superseded-by-#279** where it described the retired user surface.

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

**D2 — a topic carries a primary model + persona.** *(model/persona override
superseded-by-#293 — see §2.)*
Originally, assigning a topic to a chat set the chat's default model and persona
via the topic's `default_model` / `default_persona` columns, injected by the
proxy at session create. **#293 retired that override:** the household gateway's
profile now owns the model + soul, so the proxy no longer injects a per-session
model override or persona overlay (the topic columns stay in the schema but are
no longer consulted at create). What survives of D2 is the **topic binding as a
tag**: a chat started under a topic is persisted as its primary assignment and
its turns get the `#topic/<slug>` context hint (#241/#242), routing ingestion —
it just no longer changes the model/persona, which the profile pins.

*Binding is at session create.* Hermes binds model + system_prompt only when a
session is born (the latency bundle — the model can't switch per-turn). Post-#293
the **profile** supplies both at create; the proxy passes neither. Changing the
primary topic on an **existing** session still updates the chip/label and future
`#topic/` ingestion tags but reuses the same Hermes session (one create), so it
never rebinds the live session — the #242 limitation, now moot for model/persona
since those are profile-owned.

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

### UI surfaces (picker era — superseded-by-#279)

These were the #241/#242 user surfaces. The **Topic picker** is *retired* by
#279 (replaced by §"Mention-based tagging" below); the chip and pinned-chat
surfaces survive in spirit (the tag-cloud and the Zuhause pin).

- ~~**Topic picker** in the chat header (alongside Schnell/Gründlich and
  persona selector).~~ — *retired (#279); replaced by inline `#tag`/`@person`
  mentions + the tag-cloud.*
- **Topic chip** in the session list (visual at-a-glance).
- **Pinned topic-chats** in the rail — pre-assigned topic + model/persona
  (the #237 pattern extended to user topics).

### Mention-based tagging (#279 — replaces the picker)

The user-facing surface is now **inline mentions** typed in the chat, not a
header picker:

- **`#tag`** — a free-form tag. **`@person`** — a person reference. Both are
  parsed out of the message text as the resident types.
- **Autosuggest while typing.** Typing `#` or `@` opens an autosuggest popover
  (the existing slash-menu pattern). `#` suggests from **already-known tags**
  (tags used before); `@` suggests from **known persons**. *Decision: build
  both `#tags` and `@persons` now* — `@person` suggestions are seeded from
  residents / the uid registry plus a manual list. CardDAV/contacts enrichment
  (#207, parked behind gbrain) extends the person suggestions *later* when it
  lands; the mention surface ships without waiting for it.
- **Tag-cloud.** The tags and persons used in a chat render as a cloud **to the
  right of the chat on desktop** (when there's room) **or as a small line
  directly above the message input** otherwise (responsive). Each tag/person in
  the cloud **links back to the message where it was used** (jump-to-message
  anchor).

**Internal vs user-facing.** This replaces only the *user-facing picker*. The
system topic **binding** (D2) survives as the internal primary-tag + context
hint: the Zuhause chat still runs `gemma4:e2b` + the household soul (now via the
household **profile**, §2 — not a per-session topic override, superseded-by-#293),
and the `topics` table keeps its system rows (`household`, `servicebay-admin`)
as internal plumbing — residents simply never pick from a topic list anymore.

**Storage (open — child units decide).** Where mentions are persisted per
chat + per message is left open by this note: either a dedicated **`tags`
table** (+ a per-message tag/person link), or **repurposing `session_topics`**
with a per-message tag link alongside it. This is a design note; the builder of
the child units picks the specifics.

**Planned decomposition (#279 child units).** This note unblocks the build,
which #279 splits into:

1. **`#tag` parse + autosuggest + store** — parse `#` mentions, suggest from
   known tags, persist them.
2. **Tag-cloud UI + jump-to-message** — the responsive cloud (desktop-right /
   mobile-line) with jump-to-message anchors.
3. **`@person` parse + seed + autosuggest** — parse `@` mentions, seed persons
   from residents / a manual list, suggest from known persons.
4. **Retire the Thema picker** — remove the `#topic-control` picker + the
   `FIXED_CONTEXT_TOPICS` gating (#274); the internal binding stays.

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

## 5. Temporary / Incognito chats

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

Mechanism: an ephemeral flag on the session (carried in the `[temp:]` title
marker alongside the topic markers); the proxy checks it before every write
path. The incognito `[temp:]` prefix + the per-turn guard hint are retained as a
per-session lever after the #293 overlay simplification (§2) — the profile owns
the soul, but incognito is still the proxy's.

---

## 6. Phasing

**v1 (no gbrain dependency):**
Topics registry + `session_topics` + per-topic primary-tag binding (model/persona
now profile-owned, §2, superseded-by-#293) +
auto-`#topic/` tagging + topic-filtered notes-search + topic suggestion +
temporary/incognito chats. The user-facing **topic picker** (#241/#242) is
*superseded by #279* — replaced by inline `#tag`/`@person` mentions +
autosuggest + the tag-cloud (see §3 "Mention-based tagging"); the internal topic
bindings stay.

**v2 (gbrain v0.43+):**
Topics become first-class graph nodes/labels. Chat→topic and data→topic
assignments become typed edges. Cross-source topic retrieval runs over the
graph. The v1 `#topic/<slug>` tags map 1:1 to graph labels — forward-compatible,
no migration of tagged notes required.

---

## 7. Cross-cutting constraints

- **Per-resident isolation** (#153) — session ownership, topic scope, and data
  writes are all resident-scoped by default.
- **Pinned-persona / marker pattern** (#229/#237) — topic assignment reuses the
  same session-marker mechanism as persona pinning. Post-#293 (§2) the soul is
  pinned by the gateway **profile**, not a per-session overlay; the marker
  pattern persists for topic + incognito tagging.
- **Notes `#tag` mechanism** — already used by media-ingestion, dynamic-skills,
  and daily-chronicle; topic tagging extends it without a new convention.
- **Minimal knobs** — one global/automatic mechanism per concern, not per-feature
  toggles. Topic routing and ephemeral flags follow this principle.

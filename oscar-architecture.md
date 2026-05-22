# O.S.C.A.R. — Architecture

> Living document. May 2026 lean reset: OSCAR is a thin household-identity-and-memory layer on top of [Hermes Agent](https://github.com/NousResearch/hermes-agent) and [ServiceBay](https://github.com/mdopp/servicebay). Everything that is not specifically about *this household* lives in those two projects.

## Vision

OSCAR is a private operating system for the family: voice at home, chat on the road, one assistant, every resident has their own world, nothing leaves the house by accident.

### Five intents

1. **Sovereignty.** Modern AI in the house without exposing the family. All AI runs locally on a household server. Cloud LLMs only on explicit, audited opt-in.
2. **Long memory.** The household's bookshelf, record collection, documents, photos, appointments, decisions — woven into something the assistant can query.
3. **One conversation.** Voice at home (Wyoming via HA Voice PE devices), chat on the road (Signal/Telegram/…) — same agent, same memory.
4. **Per-resident privacy.** Father, mother, child each have their own memory namespace and tool scope. Guests get a smaller, locked-down world. *Voice is identity.*
5. **Things actually happen.** Lights, heating, scenes, timers, reminders — driven through Home Assistant's MCP server.

## The boundary

Three projects, three jobs. Whenever a capability is generic, it lives in one of the other two and OSCAR consumes it.

### What [Hermes Agent](https://github.com/NousResearch/hermes-agent) gives us

Hermes is the **agent runtime**. Consumed as the upstream container `docker.io/nousresearch/hermes-agent`. As of May 2026 it ships far more than an earlier draft of this document assumed — several things OSCAR planned to build are already native:

- Conversation loop, skill registry, agent-curated skill creation, self-improvement loop, 70+ built-in tools
- **20+ messaging gateways** (Signal, Telegram, Discord, Slack, WhatsApp, Email, Matrix, …) — paired interactively via `hermes gateway setup`
- **Native Home Assistant integration** — a gateway plus four device-control tools (`light`, `switch`, `climate`, `cover`, `media_player`, `fan`, `scene`, `script`, …). Setup is a `HASS_TOKEN` long-lived access token. **This fully covers OSCAR intent 5; OSCAR builds nothing for HA control.** ([docs](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/homeassistant))
- **Voice mode** — local STT (faster-whisper, zero API keys) + TTS (Edge/NeuTTS local, or cloud). Shapes: CLI voice, gateway voice replies, Discord voice channels. **It does not speak Wyoming and does not drive HA Voice PE devices** — that distributed-satellite path is what OSCAR's gatekeeper adds. ([docs](https://hermes-agent.nousresearch.com/docs/user-guide/features/voice-mode))
- **Honcho memory** — per-user "peer" profiles with automatic user modelling, semantic search, session strategies. Per-resident memory separation is largely a Honcho-peer concern, not an OSCAR-Qdrant-namespace concern. ([docs](https://hermes-agent.nousresearch.com/docs/user-guide/features/honcho))
- **Skills ecosystem** — `hermes skills install` against a curated hub (agentskills.io standard, 17 categories, security-scanned). Notably `official/research/qmd`: a local hybrid-retrieval engine (BM25 + vector + LLM rerank) over a markdown knowledge base — relevant to OSCAR intent 2.
- Cron scheduler for timers, alarms, reminders, recurring tasks
- MCP client for consuming external tool surfaces
- LLM-provider abstraction (local via Ollama, cloud via Claude / Gemini / OpenRouter / …)

OSCAR does **not** fork Hermes. Behaviour we miss gets contributed back as a PR or as an MCP server Hermes can mount. **Before building anything, check whether Hermes already ships it** — the surface grows fast. The fastest-path walkthrough in [`docs/getting-started.md`](docs/getting-started.md) shows how much of OSCAR's promise is reachable from a bare `pip install hermes-agent`.

### What [ServiceBay](https://github.com/mdopp/servicebay) gives us

ServiceBay is the **platform**. Consumed as an external template registry. ServiceBay provides:

- Identity: LLDAP + Authelia (full-stack)
- Smart home: Home Assistant (full-stack, **without** the bundled Wyoming pipeline). Hermes reaches HA through its **native HA integration** (a `HASS_TOKEN`), not through HA's MCP server — simpler, and it makes HA control a property of the `hermes` template's environment, not of OSCAR.
- Photos / calendar / contacts / audiobooks / music / file-share (full-stack: `immich`, `radicale`, `media`, `file-share`)
- Reverse proxy + TLS (`nginx`), DNS sinkhole (`adguard`), password manager (`vaultwarden`)
- Platform MCP control surface (`/mcp`, scopes `read|lifecycle|mutate|destroy`, bearer token)
- Existing `voice` template (Whisper + Piper + openWakeWord, shipped via [`mdopp/servicebay#348`](https://github.com/mdopp/servicebay/issues/348)) — deployed unchanged alongside `oscar-household` once OSCAR adds voice
- Structured-logging surface (`src/lib/logger.ts`, SQLite-backed, shape `{ts, level, tag, message, args}`) — OSCAR containers emit one JSON line per event matching this contract
- Health-check system (v3.35–v3.37, 16 check types, MCP tools `create_health_check` / `get_health_checks` / `run_check_now` / `diagnose`) — OSCAR's `oscar-status` skill consumes those tools instead of implementing its own probes
- New `ai-stack` walkthrough + new `ollama` and `hermes` templates **to be built** in `mdopp/servicebay` (tracked in [`#538`](https://github.com/mdopp/servicebay/issues/538), [`#539`](https://github.com/mdopp/servicebay/issues/539), [`#540`](https://github.com/mdopp/servicebay/issues/540))

Phase-3a additions (`postgres`, `qdrant`) are deferred to *when* Phase 3a is built; storage choice is re-opened then.

### What's left for OSCAR

After honestly subtracting everything Hermes now ships natively, the irreducible household-specific layer is:

1. **Voice ↔ resident identity.** Speaker embedding (SpeechBrain ECAPA-TDNN) → LLDAP `uid` lookup → the right Honcho peer for that resident. Phase 2. Hermes has no concept of "which human is speaking" — this is the deepest OSCAR-eigen piece.
2. **The gatekeeper image:** Wyoming-protocol bridge that connects HA Voice PE devices to Hermes. Hermes' own voice mode is CLI/Discord only — it does not speak Wyoming or drive room satellites. Published as `ghcr.io/mdopp/oscar-gatekeeper`, runs inside the `oscar-household` pod (both pods `hostNetwork: true`, gatekeeper reaches `voice`'s Whisper/Piper via host loopback). Long-term target: contribute the pass-through path to Hermes as a generic `hermes gateway voice`.
3. **Cloud-LLM audit policy.** A small SQLite database (`cloud_audit`, `system_settings`, `voice_embeddings`) plus three skills: `oscar-status`, `oscar-audit-query`, `oscar-debug-set`. "Every cloud call is family-visible" is a policy, not a Hermes feature. SQLite file in the `oscar-household` volume — zero external infrastructure.
4. **A ServiceBay stack walkthrough + the `oscar-household` template** — German-household defaults, the schema-init sidecar, the gatekeeper container, and the deterministic path from "ServiceBay installed" to a running household assistant.

Everything else from earlier OSCAR drafts is upstreamed, dropped, or postponed: data-plane templating, Hermes-container wrapping, voice-pipeline templating, weather connectors, structured-logging library, health-probe library, **the `oscar-light` skill (Hermes' native HA integration replaces it outright — not even an upstream candidate)**, the connector skeleton. The Phase-3a ingestion module and domain-collection schema are **re-opened**: Hermes' `qmd` skill plus Honcho may cover "long memory" with markdown notes, making a custom database unnecessary — see the Phase 3a section.

## Architecture overview

```
   SIGNAL / TELEGRAM / DISCORD / …                                 HA Voice PE
              │                                                           │ Wyoming
              │  Hermes-native gateway                                    ▼ <host>:10700
              ▼                                                  ┌──────────────────┐
   ┌──────────────────────────┐                                  │  oscar-household │
   │     hermes               │                                  │  (OSCAR)         │
   │     (ServiceBay)         │◄────────HTTP /converse───────────┤                  │
   │     wraps nousresearch/  │                                  │  • gatekeeper    │  Wyoming
   │     hermes-agent         │                                  │    container     │◄──host
   │                          │                                  │    (Wyoming↔HTTP)│  loop-
   │  Honcho + FTS5 (SQLite)· │                                  │  • SQLite +      │  back
   │  cron · skill registry · │                                  │    Alembic       │  ┌─────────────────────┐
   │  MCP client              │                                  │    (oscar.db)    │  │  voice (ServiceBay) │
   └──┬──────┬──────────┬─────┘                                  │  • mounts OSCAR  │  │  whisper + piper +  │
      │      │          │ MCP                                    │    skills        │  │  openwakeword       │
      │      │                                                  │  • non-interactive  └─────────────────────┘
      │      │                                                  │    post-deploy    │
      │      ▼ native HA gateway          ▼ MCP                  │    registers      │
      │  ┌───────────────┐  ┌─────────────────────────┐         │    ServiceBay-MCP │
      │  │ Home Assistant│  │  ServiceBay-MCP         │         └──────────────────┘
      │  │ (HASS_TOKEN — │  │  (services, health,     │
      │  │  native, not  │  │   logs, diagnostics)    │
      │  │  an MCP)      │  └─────────────────────────┘
      │  └───────────────┘
      │
      ▼ skills read-mount
      │   /opt/data/skills/oscar:
      │     • oscar-status
      │     • oscar-audit-query
      │     • oscar-debug-set
      ▼ Hermes LLM-provider URL
   ┌─────────────────────────────────┐
   │  ollama (ServiceBay, ai-stack)  │
   │  local Gemma                    │
   └─────────────────────────────────┘
```

Two templates from ServiceBay's `ai-stack` (`ollama`, `hermes`), one existing ServiceBay template (`voice`, used as-is — Phase 1), one OSCAR template (`oscar-household`). Home Assistant is reached through Hermes' **native HA integration** (`HASS_TOKEN` on the `hermes` template), not via an MCP server. The gatekeeper container — OSCAR-published image — runs **inside the oscar-household pod**, both pods sharing the host netns so the gatekeeper reaches the `voice` template's Whisper/Piper on `127.0.0.1` and Hermes on `127.0.0.1:8642`. OSCAR's three tables live in a SQLite file in `oscar-household`'s volume — no external Postgres for Phase 0–2.

## Components in detail

### gatekeeper (OSCAR-published image)

A Wyoming-protocol server. **Runs as a container inside the `oscar-household` pod**, not as a sidecar of ServiceBay's `voice` template. Both pods are `hostNetwork: true`, sharing the host netns, so the gatekeeper reaches Whisper, Piper, and Hermes through `127.0.0.1`.

One inbound satellite connection = one half-duplex pipeline turn:

1. HA Voice PE (or any `wyoming-satellite` client) connects on `<host>:<GATEKEEPER_PORT>` and streams `AudioStart` + `AudioChunk*` + `AudioStop`.
2. The gatekeeper calls the `voice` template's Whisper container at `tcp://127.0.0.1:10300` for STT.
3. *Phase 0/1:* `uid = DEFAULT_UID`. *Phase 2:* SpeechBrain ECAPA-TDNN extracts a 256-d voice embedding; lookup against `voice_embeddings` in OSCAR's SQLite (3–10 vectors per family — brute-force cosine in Python) resolves it to an LLDAP `uid`; unknown → `guest`.
4. The gatekeeper POSTs `(text, uid, endpoint, trace_id)` to Hermes' API at `HERMES_URL` (default `http://127.0.0.1:8642`).
5. Hermes' response text goes to Piper at `tcp://127.0.0.1:10200`; synthesised audio streams back to the satellite.
6. Outbound: `POST /push {endpoint: "voice-pe:<name>", text}` lets Hermes' cron and proactive deliveries address a specific Voice PE device by name (resolved against `VOICE_PE_DEVICES`).

Voice embeddings live in OSCAR's SQLite (`voice_embeddings` table in `oscar.db`, mounted into the gatekeeper container at `/var/lib/oscar/oscar.db`, FK to LLDAP `uid`); **never** in LLDAP — biometric PII.

Long term, the Phase-0 pass-through path (steps 1, 2, 4, 5) should land in Hermes as a generic `hermes gateway voice`. The OSCAR-specific Phase 2+ logic (speaker ID, multi-room routing, voice-tone analysis) stays in `oscar-household`.

### oscar-household (OSCAR template)

The one ServiceBay template OSCAR ships. Pod with two containers (`oscar-household-init`, `gatekeeper`), shared volume `/var/lib/oscar`, `hostNetwork: true`, declares `servicebay.dependencies: "hermes,voice"`. Responsibilities:

- **Schema init.** One-shot Alembic container (`ghcr.io/mdopp/oscar-household-init`) runs on every pod start against `oscar.db` in the bind-mounted volume; creates `cloud_audit`, `system_settings`, `voice_embeddings` and, in Phase 3a, the domain-collection tables. Idempotent.
- **Skill mount.** OSCAR's `skills/` directory (cloned with the OSCAR registry into the same volume) is mounted into the Hermes container at `/opt/data/skills/oscar` by ServiceBay's `hermes` template, sharing the hostPath. The OSCAR skills read `oscar.db` directly from `/var/lib/oscar/oscar.db`.
- **Voice gatekeeper.** Long-running container with the OSCAR-published `gatekeeper` image. Reaches the `voice` template's Wyoming services and Hermes via host loopback (both pods are hostNetwork). Speaks Wyoming to satellites on `<host>:<GATEKEEPER_PORT>`.
- **MCP wiring.** Non-interactive `post-deploy.py` reads `${DATA_DIR}/hermes/config.yaml` (the file ServiceBay's `hermes` template wrote with the `model:` block), splices in an `mcp_servers:` block for ServiceBay-MCP, writes back, then `POST /api/services/hermes/action {action: "restart"}` so Hermes picks up the new config. Sources: [Hermes MCP Config Reference](https://hermes-agent.nousresearch.com/docs/reference/mcp-config-reference), [Hermes Configuration](https://hermes-agent.nousresearch.com/docs/user-guide/configuration). **HA is *not* wired here** — it's a native Hermes gateway driven by a `HASS_TOKEN`, which belongs in the ServiceBay `hermes` template's environment (open coordination point with `mdopp/servicebay#544`: the `hermes` template should grow optional `HASS_TOKEN` / `HASS_URL` variables). Caveat: re-deploying the `hermes` template rewrites `config.yaml` with just the `model:` block, so re-deploy `oscar-household` to restore the `mcp_servers:` block.
- **Audit hook (pending).** Once `mcp-audit-proxy` ships as its own MCP server, the post-deploy will also register it with Hermes so every cloud-LLM call writes a `cloud_audit` row.
- **Variables.** Wizard-prompted (see `templates/oscar-household/variables.json`): `DEFAULT_UID`, `GATEKEEPER_IMAGE`, `GATEKEEPER_PORT`, `WHISPER_URI` / `PIPER_URI` / `OPENWAKEWORD_URI`, `VOICE_PE_DEVICES`, `HERMES_API_PORT` / `HERMES_API_KEY` (paste from the ServiceBay `hermes` template's `__SB_CREDENTIAL__` banner), `PUSH_TOKEN`, `SERVICEBAY_MCP_URL` / `SERVICEBAY_MCP_TOKEN`, `LLDAP_GROUP`, `OSCAR_DEBUG_MODE`, `TZ`. *(The `HA_MCP_*` variables from an earlier draft are dropped — HA is the `hermes` template's `HASS_TOKEN`, not OSCAR's concern.)*

### The three skills

| Skill | Reads | Purpose |
|---|---|---|
| `oscar-status` | ServiceBay-MCP `get_health_checks` / `diagnose` + an `oscar.db` probe | "Is OSCAR alive?" — answered by the platform's health surface. |
| `oscar-audit-query` | `cloud_audit` table | "What did the cloud connector send today?" — natural-language read over the audit table. |
| `oscar-debug-set` | `system_settings.debug_mode` row | Admin only. Voice toggle for verbose mode with a TTL ("debug on for four hours"). |

Each skill is a small Hermes skill (Markdown spec + Python tool calls), tracked in `skills/` and read-mounted via `oscar-household`.

### The schema

Three tables in Phase 0–2, all in a single SQLite file (`oscar.db` in the `oscar-household` volume):

| Table | Owner | Notes |
|---|---|---|
| `cloud_audit` | OSCAR | One row per Hermes cloud-LLM call: timestamp, uid, trace_id, vendor, model, prompt-hash, prompt-length, response-length, cost, router score, escalation reason. Full text gated by `system_settings.debug_mode`. Tens of rows per day; thousands per year. |
| `system_settings` | OSCAR | A single row with global flags: `debug_mode.active`, `debug_mode.verbose_until` (TTL), `debug_mode.latency_annotations`. Read by every component on every audit event (no caching > 5 s). |
| `voice_embeddings` | OSCAR | 256-d ECAPA-TDNN vectors per LLDAP uid + enrolment metadata. Phase 2. FK to LLDAP `uid` (string). 3–10 rows total — k-NN done brute-force in Python, no vector index needed. |

Phase 3a **re-opens** the domain-collection question entirely (see the Phase 3a roadmap section). Hermes' `qmd` skill already does local hybrid retrieval over a markdown knowledge base — a "book I read" or "decision we made" can be a markdown note `qmd` indexes, with no `books`/`records`/`documents` tables at all. A custom schema only earns its place if structured queries (filter by ISBN, rating, status) turn out to matter more than free-text retrieval. That is a Phase-3a decision, made with real usage in hand, not now.

Alembic migrations live in `schema/`; the migration container is part of `oscar-household`. The migration model is portable to Postgres should a Phase-3a schema ever require it — one-day migration with `INSERT … SELECT`.

## Identity and harness (Phase 2)

Three harness types compose at runtime: **System** (always active) ∪ (**Personal** | **Guest**). One YAML per LLDAP `uid` (e.g. `michael.yaml`), checked in once Phase 2 starts and the directory layout is decided. Five fields per harness: `context`, `tools`, `guides`, `sensors`, `permissions`.

Until Phase 2 ships, harness composition is layered on top of Hermes' own user/skill knobs by the gatekeeper at turn-handoff time, with `uid = DEFAULT_UID` as the placeholder.

Whether harness composition becomes a Hermes-upstream feature (so any multi-user Hermes deployment can use it) or stays an OSCAR-side wrapper is a Phase-2 decision.

## Memory layers

| Layer | Storage | Where | Owner |
|---|---|---|---|
| Conversation history + per-resident user modelling | Honcho (per-user **peers**) + FTS5 SQLite | Hermes container's data volume | Hermes |
| Document / note retrieval | `qmd` skill's local index (BM25 + vector + rerank) over a markdown folder | Hermes container's data volume | Hermes skill |
| OSCAR audit | SQLite (`oscar.db`) | `oscar-household` container's volume | OSCAR |

Per-resident memory separation is largely a **Honcho-peer** concern: once the gatekeeper resolves which resident is speaking (Phase 2), it hands Hermes the matching peer and Honcho keeps the worlds apart on its own. OSCAR does not build a Qdrant-namespace layer for this. A dedicated vector store enters the picture only if a Phase-3a structured-collection design needs it — see the schema section.

## Cross-cutting concerns

### Debug mode

Global switch in the `system_settings.debug_mode` row in `oscar.db`. While building OSCAR (Phase 0–2) it defaults to `on`; productive household use flips it to `off`.

When `active = true`:

- All components log full text (prompts, responses, tool args, connector bodies) instead of metadata only
- Retention policies on audit tables are suspended
- With `latency_annotations: true`, voice responses carry "STT 230ms · router 80ms → local 12B · 1.4s" annotations (filtered to admin uids)

TTL via `verbose_until`. Components re-query the row on every audit event (no caching > 5 s). Voice toggle via the `oscar-debug-set` skill: *"Debug mode on for four hours"* → sets `active=true, verbose_until=now()+4h`.

### Cloud-LLM audit policy

Every Hermes cloud-LLM call generates a `cloud_audit` row. The audit mechanism is an MCP audit-proxy (small project, separate repo — *see upstream work*). The *policy* — "every cloud call is family-visible" — is OSCAR-eigen: it shows up in the `oscar-audit-query` skill, in the family's view of what the assistant has been doing, and in the per-harness cloud opt-in.

### Logging

Operational: container stdout JSON → journald → ServiceBay's logger (`src/lib/logger.ts`, SQLite-backed). ServiceBay already shapes logs as `{ts, level, tag, message, args}`, queryable via `/api/logs/query?level=…&tag=…&search=…`. OSCAR containers emit one JSON line per event matching that contract; the doc is [`mdopp/servicebay#542`](https://github.com/mdopp/servicebay/issues/542).

Domain audit: SQLite tables in `oscar.db`, read via the `oscar-audit-query` skill.

Conversation: Hermes-native under its data volume.

Correlation via `trace_id` per turn.

**Health checks:** ServiceBay shipped a full health system in v3.35–v3.37 (16 check types: `http`, `ping`, `script`, `podman`, `service`, `systemd`, …). Model is outside-in — templates declare what should be probed; the platform polls and aggregates. The OSCAR `oscar-status` skill calls ServiceBay-MCP's `get_health_checks` / `diagnose` tools; no per-container `/health` endpoint needed. Doc gap tracked in [`mdopp/servicebay#543`](https://github.com/mdopp/servicebay/issues/543).

## Phase roadmap

> **Tiered entry point.** [`docs/getting-started.md`](docs/getting-started.md) splits "running assistant" into three tiers. Tier 1 (Hermes alone) and Tier 2 (Hermes + native HA + `qmd` + Signal + voice) deliver four of the five intents from a bare `pip install`, with no OSCAR code and no ServiceBay template. Phase 0 below *is* Tier 3 — the packaged household deployment. Validate Tiers 1–2 hands-on first; findings there can shrink the phases below.

### Phase 0 — Household deployment (chat + HA control)

**Prereqs.** ServiceBay v3.16+ with the full-stack deployed. `mdopp/servicebay#348` merged (HA without bundled Wyoming) — *only needed once voice is added*. `mdopp/servicebay#443` merged (`git` in ServiceBay's container) so the OSCAR registry can be cloned.

**Deliverables.** ServiceBay's `ollama` and `hermes` templates exist and are wizard-deployable. OSCAR's `oscar-household` template exists and ships its own SQLite. `ai-stack` walkthrough plus OSCAR's stack walkthrough together produce a working setup. The `hermes` template carries `HASS_TOKEN` / `HASS_URL` so Hermes' **native HA integration** is live on first boot — device control needs no OSCAR code. Operator pairs Signal once via `podman exec -it hermes signal-cli link -n "HermesAgent"` (genuinely interactive — QR scan). `oscar-household`'s post-deploy registers ServiceBay-MCP via a `config.yaml` merge + restart, and initialises `oscar.db`.

**Result.** Family chat in Signal, full HA device control, conversation memory, cloud-call audit. No room-voice path yet (Discord-voice works as the Tier-2 interim).

### Phase 1 — Voice path

**Prereqs.** ServiceBay's existing `voice` template (Whisper + Piper + openWakeWord, shipped via [`mdopp/servicebay#348`](https://github.com/mdopp/servicebay/issues/348)) deployed alongside `oscar-household`. Both pods are `hostNetwork: true` so the gatekeeper container in `oscar-household` reaches Whisper/Piper through the host loopback. OSCAR's gatekeeper image published to `ghcr.io/mdopp/oscar-gatekeeper`.

**Deliverables.** HA Voice PE in the office, configured against `<host>:<GATEKEEPER_PORT>` (the port the gatekeeper container in `oscar-household` exposes). Gatekeeper in pass-through mode (`DEFAULT_UID`). Whisper-large-v3 on GPU (≤50 ms for 3 s audio). Piper for German voice.

**Result.** Spoken conversation at home, single-user. Same agent and same memory as the Signal channel.

### Phase 2 — Speaker ID + per-resident namespaces

**Deliverables.** SpeechBrain ECAPA-TDNN in the gatekeeper. `voice_embeddings` table populated via an enrolment wizard. Harness YAML schema. `system.yaml` + `michael.yaml` + `guest.yaml`. The gatekeeper resolves uid per turn; Hermes runs under that uid's memory namespace and tool scope.

**Result.** Per-resident privacy. Voice is identity.

### Phase 3a — Streaming ingestion (design re-opened)

**The premise has changed.** An earlier draft specified custom domain-collection tables (`books`, `records`, `documents`, `audiobooks`, `experiences`) plus a Qdrant index. Hermes' `qmd` skill now does local hybrid retrieval (BM25 + vector + LLM rerank) over a markdown knowledge base out of the box. So the **first question of Phase 3a is no longer "what schema" — it's "do we need a schema at all"**:

- *qmd-first path:* household material becomes markdown notes in a `qmd`-indexed folder. A book is a note; a receipt is a note with its key facts. Ingestion = "turn an inbound photo/file into a markdown note." No tables, no Qdrant, no Alembic domain migrations. Retrieval is `qmd`.
- *Schema path:* only if structured queries (filter by ISBN / rating / status, join across collections) prove to matter more than free-text retrieval. Then — and only then — the `books`/`records`/… tables and a vector store come back.

**Deliverables (qmd-first, the working assumption).** An ingestion flow: trigger from Hermes messaging-gateway attachments **or** a Syncthing-watched per-uid inbox; classify + extract with the local multimodal model; write a structured markdown note; let `qmd` index it. Confirmation dialogue before filing. Encrypted material store for originals on a dedicated mount.

**Result.** Long memory begins — measured against `qmd` retrieval quality before any database is built.

### Phase 3b — Bulk import + MCP wrappers

Signal/Telegram history import. Google Takeout. Audiobookshelf, Immich, Radicale wrappers as MCP tools Hermes consumes.

### Phase 4 — Active extensions

Voice-tone analysis as a parallel gatekeeper sensor. Multi-room voice routing (≥2 rooms). Custom "Oscar" wakeword. Proactive Hermes-driven memo creation. TuneIn / internet-radio MCP. Multi-household.

## Upstream work tracked from OSCAR

| Where | What | Phase | Status |
|---|---|---|---|
| [`mdopp/servicebay#538`](https://github.com/mdopp/servicebay/issues/538) | New `ollama` template with optional GPU passthrough (default-bind `127.0.0.1` + NPM/Authelia for remote access) | 0 | Open, amended |
| [`mdopp/servicebay#539`](https://github.com/mdopp/servicebay/issues/539) | New `hermes` template wrapping `docker.io/nousresearch/hermes-agent`. Non-interactive setup (no `podman exec`), `dependencies: ollama` annotation, dashboard default-binds `127.0.0.1` | 0 | Open, amended |
| [`mdopp/servicebay#540`](https://github.com/mdopp/servicebay/issues/540) | New `ai-stack` walkthrough bundling Ollama + Hermes | 0 | Open |
| [`mdopp/servicebay#541`](https://github.com/mdopp/servicebay/issues/541) | ~~Extend `voice` template with `GATEKEEPER_IMAGE` sidecar~~ | ~~1~~ | **Closed** — gatekeeper lives in `oscar-household` instead |
| [`mdopp/servicebay#542`](https://github.com/mdopp/servicebay/issues/542) | `docs/TEMPLATE_LOGGING.md` describing the existing `{ts, level, tag, message, args}` shape (ServiceBay's logger already produces it) | any | Open, doc-only |
| [`mdopp/servicebay#543`](https://github.com/mdopp/servicebay/issues/543) | `docs/TEMPLATE_AUTHORING.md` health-checks section pointing at the existing 16-check-type system (v3.35–v3.37) | any | Open, doc-only |
| `mdopp/servicebay` | New `postgres` + `qdrant` templates | 3a (conditional) | Not yet filed — only if Phase 3a chooses migration off SQLite |
| `mdopp/servicebay` | `hermes` template grows optional `HASS_TOKEN` / `HASS_URL` variables so Hermes' native HA integration is live on first boot | 0 | Not yet filed — coordinate on `#544` |
| `NousResearch/hermes-agent` | Voice gateway: contribute the Phase-1 gatekeeper pass-through path as `hermes gateway voice` | 1+ | Not yet filed (waits for Phase-1 deploy validation) |
| New separate repo | `mcp-audit-proxy` — generic cloud-LLM auditing MCP; OSCAR provides only the policy + schema | 0 | Not yet created |

The `smart-home/home-assistant` skill that an earlier draft planned to upstream is **dropped** — Hermes ships a native HA integration that supersedes it. `oscar-light` is deleted, not contributed.

Tracking issue [`mdopp/oscar#70`](https://github.com/mdopp/oscar/issues/70) links to all of the above.

## Key decisions

| Topic | Decision |
|---|---|
| **Agent runtime** | Hermes Agent (`docker.io/nousresearch/hermes-agent`), unforked, deployed via ServiceBay's `hermes` template |
| **Platform** | ServiceBay v3.16+ on Podman Quadlet, Fedora CoreOS host |
| **AI infrastructure** | ServiceBay `ai-stack` (Ollama + Hermes for Phase 0; Postgres + Qdrant conditional in Phase 3a); not OSCAR's job to deploy |
| **Storage** | SQLite for Phase 0–2 — single `oscar.db` in `oscar-household`'s volume. Consistent with how Hermes stores Honcho. Postgres + Qdrant is a Phase-3a decision, not a baked-in dependency. |
| **Identity** | LLDAP `uid` + groups from ServiceBay's `auth` pod; SSO via Authelia OIDC for any OSCAR web UI |
| **Voice pipeline** | ServiceBay's unchanged `voice` template (Whisper + Piper + openWakeWord) deployed alongside `oscar-household`. The gatekeeper container lives **inside** `oscar-household`, not as a sidecar of `voice` — both pods are `hostNetwork: true` so the gatekeeper reaches Wyoming services through the host loopback. |
| **Voice identity** | Speaker embedding in the gatekeeper → uid lookup in OSCAR's SQLite `voice_embeddings` table → never in LLDAP |
| **Messaging gateways** | Hermes-native (Signal, Telegram, Discord, Slack, WhatsApp, Email, +14 more). Paired via `hermes gateway setup`. No OSCAR-side gateway code. |
| **HA device control** | Hermes' **native** HA integration (`HASS_TOKEN` on the `hermes` template) — a gateway + four device-control tools. Not HA-MCP, not an OSCAR skill. The `oscar-light` skill is deleted. |
| **Document / note memory** | Hermes' `qmd` skill (local BM25 + vector + rerank over markdown). A custom Phase-3a database is re-opened as a question, not a given. |
| **Timers / alarms / reminders** | Hermes-native cron scheduler. No OSCAR table. |
| **Memory** | Hermes Honcho (conversation + per-resident peers) + `qmd` (documents) in Hermes' volume; OSCAR's `oscar.db` (audit only) in `oscar-household`'s volume. |
| **Cloud LLM** | Off by default; opt-in per harness. Every call writes to `cloud_audit`. Family-visible via `oscar-audit-query`. |
| **Audit-proxy mechanic** | Separate repo / package (`mcp-audit-proxy`), not OSCAR-eigen; OSCAR contributes only the policy + the schema |
| **Hardware** | GPU server (RTX 4070 or comparable, ≥12 GB VRAM). No CPU-only path for live voice. |
| **Hermes core modding** | None. Capabilities we miss get PR'd upstream or added as MCP servers. |
| **OSCAR template count** | One (`oscar-household`). Anything more is a smell that we're rebuilding ServiceBay or Hermes. |
| **gatekeeper home** | OSCAR-published image; long-term target is to land the Phase-0 pass-through path in Hermes |
| **Phase 0 trigger** | Working Signal chat with HA control. Voice path is Phase 1, not Phase 0. |
| **Entry point** | [`docs/getting-started.md`](docs/getting-started.md) — Tiers 1–2 (`pip install hermes-agent` + config) reach four of five intents with no OSCAR code; Phase 0 is the packaged Tier-3 household deployment. |

## Open points

1. **Gatekeeper migration path.** Once the voice gateway lands in Hermes upstream, the gatekeeper image shrinks to "OSCAR-specific extensions" only (speaker ID, multi-room, voice-tone). Timeline depends on Nous review cadence.
2. **harness composition home.** Phase-2 question: does the system + uid + guest composition layer live in OSCAR (a small service the gatekeeper consults before posting to Hermes) or as a contributed feature in Hermes itself?
3. **Material-store encryption.** Phase 3a. LUKS container vs. filesystem-layer (gocryptfs). Key management (TPM, boot-time passphrase, Authelia-protected unlock UI?).
4. **MCP wrappers for ServiceBay stack apps.** Phase 3b. Do `immich-search`, `radicale-cal`, `audiobookshelf-list` live in OSCAR or as standalone MCP servers in their own repos?

## Sources

- Hermes Agent — <https://github.com/NousResearch/hermes-agent>
- Hermes Agent docs — <https://hermes-agent.nousresearch.com/docs/>
- ServiceBay — <https://github.com/mdopp/servicebay>
- agentskills.io — <https://agentskills.io/>
- Wyoming Protocol — <https://github.com/rhasspy/wyoming>
- HA MCP server — <https://www.home-assistant.io/integrations/mcp_server/>
- Harness engineering (Böckeler/Fowler) — <https://martinfowler.com/articles/harness-engineering.html>
- LLDAP — <https://github.com/lldap/lldap>
- Authelia — <https://www.authelia.com/>
- Honcho — <https://github.com/plastic-labs/honcho>
- Model Context Protocol — <https://modelcontextprotocol.io/>

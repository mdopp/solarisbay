# O.S.C.A.R.

> A privacy-first, fully-local home assistant for a family household. The brain doesn't leave the house.

## What OSCAR is for

Five intents — short version:

1. **Sovereignty.** Use modern AI without exposing the family. Everything runs on a household server; cloud LLMs only on explicit, audited opt-in.
2. **Long memory.** Books, records, documents, photos, appointments, decisions — woven together so OSCAR remembers what the household remembers.
3. **One conversation.** Voice at home, chat (Signal/Telegram) on the road — same agent, same memory.
4. **Per-resident privacy.** Father, mother, child each have their own world; guests get a smaller, locked-down one. Voice is identity (Phase 2).
5. **Things actually happen.** Lights, heating, scenes, timers, reminders — OSCAR drives Home Assistant via its MCP server.

OSCAR is **not** a from-scratch agent and not a platform. It's a thin household-identity-and-memory layer on top of two upstream projects:

- **[Hermes Agent](https://github.com/NousResearch/hermes-agent)** (Nous Research) is the agent runtime — conversation, skills, gateways, cron, memory, self-improvement, MCP client.
- **[ServiceBay](https://github.com/mdopp/servicebay)** is the platform — LLDAP/Authelia identity, Home Assistant, Immich, Radicale, media, file-share, nginx, AdGuard, Vaultwarden, Podman-Quadlet runtime on Fedora CoreOS, MCP control surface.

Anything in OSCAR that *isn't* specifically about *this household* either gets contributed back to one of those projects or replaced by an upstream equivalent. Full rationale: [`oscar-architecture.md`](oscar-architecture.md).

## How it's put together today

```
   SIGNAL / TELEGRAM / DISCORD / …                                HA Voice PE (Phase 1)
              │                                                          │ Wyoming
              │  Hermes-native gateway                                   ▼ <host>:10700
              ▼                                                 ┌──────────────────┐
   ┌──────────────────────────┐                                 │  oscar-household │
   │   hermes                 │                                 │  (OSCAR)         │
   │   (ServiceBay)           │◄────HTTP /converse──────────────┤                  │
   │                          │                                 │  • gatekeeper    │ Wyoming
   │   • skill registry       │                                 │    container     │◄── host
   │   • cron / reminders     │                                 │    (Wyoming↔HTTP)│   loop-
   │   • Honcho memory        │                                 │  • SQLite +      │   back
   │   • MCP client           │                                 │    Alembic       │  ┌──────────────────┐
   │   • self-improvement     │                                 │    (oscar.db)    │  │ voice            │
   └──┬──────┬────────────────┘                                 │  • mounts OSCAR  │  │ (ServiceBay)     │
      │      │                                                  │    skills        │  │ whisper + piper +│
      │      ▼ native HA gateway      ▼ MCP                      │  • non-interact. │  │ openwakeword     │
      │  ┌───────────────┐  ┌─────────────────────────┐         │    post-deploy   │  └──────────────────┘
      │  │ Home Assistant│  │  ServiceBay-MCP         │         │    registers      │
      │  │ (HASS_TOKEN,  │  │  (services, health,     │         │    ServiceBay-MCP │
      │  │  native — not │  │   logs, diagnostics)    │         └──────────────────┘
      │  │  an MCP)      │  └─────────────────────────┘
      │  └───────────────┘
      ▼ Hermes LLM-provider URL
   ┌─────────────────────────────────┐
   │  ollama (ServiceBay, ai-stack)  │
   │  local Gemma                    │
   └─────────────────────────────────┘
```

One OSCAR template (`oscar-household` — schema init + skill mount + gatekeeper container + non-interactive MCP wiring), one OSCAR-published image (`gatekeeper`), three OSCAR skills (`oscar-status`, `oscar-audit-query`, `oscar-debug-set`), a small SQLite database for our tables. The voice path uses ServiceBay's unchanged `voice` template alongside `oscar-household` — both pods are `hostNetwork: true`, the gatekeeper reaches Whisper/Piper via host loopback. Everything else is upstream.

## What works today

| Capability | How it's done | Phase |
|---|---|---|
| Conversation via Signal/Telegram/Discord/Slack/WhatsApp/Email/… | Hermes' built-in gateways (20+), paired interactively | Hermes-native |
| Local LLM (Gemma family) | Ollama in ServiceBay's `ai-stack`, Hermes points its provider at the Ollama port | Hermes-native |
| Cloud LLM (Anthropic, Google, OpenRouter, …) | Hermes' built-in providers; OSCAR adds the per-call `cloud_audit` row | Hermes + OSCAR |
| Light/heating/scenes/media via Home Assistant | Hermes' **native HA integration** — a gateway + four device-control tools, `HASS_TOKEN` setup | Hermes-native |
| Document / note retrieval | Hermes' `qmd` skill — local BM25 + vector + rerank over markdown | Hermes-native |
| Timers / alarms / reminders / recurring tasks | Hermes' native cron scheduler | Hermes-native |
| Cloud-LLM audit ("Was kostete der gestrige Call?") | `oscar-audit-query` skill reads `cloud_audit` | OSCAR |
| Debug-mode toggle ("Verboser Log für eine Stunde") | `oscar-debug-set` skill flips `system_settings.debug_mode` | OSCAR |
| Health check ("Is OSCAR alive?") | `oscar-status` skill reads ServiceBay-MCP's health surface | OSCAR |
| Voice — CLI + Discord voice channels | Hermes' voice mode (faster-whisper local STT, local TTS) | Hermes-native |
| Voice in the house (Wyoming + HA Voice PE pucks) | ServiceBay's `voice` template + OSCAR's `gatekeeper` container in `oscar-household` | OSCAR (Phase 1) |

Most of this is reachable **today** from a bare `pip install hermes-agent` — see [`docs/getting-started.md`](docs/getting-started.md). OSCAR's own contribution is the audit policy, the household packaging, and the room-voice gatekeeper.

## Phase plan

| Phase | Scope | Status |
|---|---|---|
| **0** | Household deployment: chat + native HA control + memory + cloud audit, packaged as the ServiceBay `ai-stack` + `oscar-household`. | code design complete; deploy pending the ServiceBay `ai-stack` templates ([#70](https://github.com/mdopp/oscar/issues/70)) |
| **1** | Room voice. HA Voice PE → gatekeeper (in `oscar-household`) → Whisper/Piper (ServiceBay's `voice` template) → Hermes. Single uid. | designed |
| 2 | Speaker ID (SpeechBrain) → LLDAP-uid → the resident's Honcho peer + tool scope. | designed |
| 3a | Ingestion — inbound photos/files become markdown notes the `qmd` skill indexes. A custom domain database is re-opened as a question, not a given. | re-scoped |
| 3b | Bulk import + MCP wrappers for Immich/Radicale/Audiobookshelf. | sketched |
| 4 | Multi-room voice, voice-tone analysis, custom "Oscar" wakeword, proactive memos. | sketched |

## Repo layout

```
oscar-architecture.md         # the architectural constitution
templates/
└── oscar-household/          # the one OSCAR ServiceBay template
gatekeeper/                   # Python source for the gatekeeper image
                              #   (published as ghcr.io/mdopp/oscar-gatekeeper,
                              #    runs as a container inside oscar-household;
                              #    reaches ServiceBay's voice template via host loopback)
schema/                       # Alembic migrations for the OSCAR tables
skills/                       # household-specific Hermes skills:
                              #   oscar-status, oscar-audit-query, oscar-debug-set
stacks/oscar/                 # ServiceBay stack walkthrough
docs/getting-started.md       # the three-tier fastest path
```

OSCAR is intentionally small. Anything bigger has either moved upstream or hasn't been built yet — see [`oscar-architecture.md`](oscar-architecture.md) for the boundary.

## Getting started

**Fastest path — try it today, no OSCAR code, no ServiceBay templates:**

```bash
pip install hermes-agent && hermes postinstall
hermes model        # pick a provider (local Ollama or a cloud key)
hermes --tui        # talk to it
```

Then layer on native HA control (`HASS_TOKEN`), the `qmd` document-retrieval skill, Signal, and voice — all Hermes-native. That's four of OSCAR's five intents from a `pip install`. Full three-tier walkthrough: [`docs/getting-started.md`](docs/getting-started.md).

**Household deployment (the packaged route):** OSCAR ships as a **ServiceBay external registry** — no install script.

1. **Prereqs**: ServiceBay v3.16+ with the full-stack deployed; [mdopp/servicebay#443](https://github.com/mdopp/servicebay/issues/443) merged (so the OSCAR registry can be cloned); [mdopp/servicebay#348](https://github.com/mdopp/servicebay/issues/348) merged (needed only once you add room voice).
2. **Two stacks**: walk through ServiceBay's `ai-stack` (Ollama + Hermes — [#544](https://github.com/mdopp/servicebay/pull/544)), then OSCAR's stack (`oscar-household` — ships its own SQLite, runs the gatekeeper container). Optional, for room voice: deploy ServiceBay's `voice` template alongside.

Full walkthrough: [`stacks/oscar/README.md`](stacks/oscar/README.md).

## Debugging with Claude Code (MCP)

The repo ships a [`.mcp.json`](.mcp.json) wiring three MCP servers into Claude Code so debug sessions can query OSCAR's state directly:

| Server | Reads | When useful |
|---|---|---|
| `oscar-sqlite` | `cloud_audit`, `system_settings` (read-only over `oscar.db`) | "Why was last night's cloud call so expensive?" |
| `oscar-servicebay` | container logs, health, services, diagnostics | "Why did the voice pod crash-loop after the last deploy?" |
| `oscar-ha` | Home Assistant entities, areas, services | "Did the office light actually turn on after that voice command?" |

Setup: copy [`.env.example`](.env.example) to `~/.config/oscar.env`, fill in real values, source it before opening the repo.

## Language

Conversation with the maintainer is German. **All versioned artefacts — docs, READMEs, code identifiers, comments, issue titles, commit messages — are English.**

## Hardware

- Single host running ServiceBay on Fedora CoreOS.
- For real-time voice: an NVIDIA GPU (≥12 GB VRAM, e.g. RTX 4070) so Whisper-large + Gemma 12B Q4 + Piper stream under 500 ms.
- For testing: CPU-only works for chat (Phase 0); not for live voice.
- HA Voice PE devices on the same LAN once you're at Phase 1.

## Contributing

Most of the open work is **not in this repo** — by design. The architecture pushes capabilities into ServiceBay and Hermes where they belong. Active upstream candidates are tracked from OSCAR's [tracking issue](https://github.com/mdopp/oscar/issues), with cross-links to:

- `mdopp/servicebay` — new `ollama` ([#538](https://github.com/mdopp/servicebay/issues/538)) and `hermes` ([#539](https://github.com/mdopp/servicebay/issues/539)) templates for Phase 0, `ai-stack` walkthrough ([#540](https://github.com/mdopp/servicebay/issues/540)), plus documentation tickets for the existing logging contract ([#542](https://github.com/mdopp/servicebay/issues/542)) and health-check system ([#543](https://github.com/mdopp/servicebay/issues/543)). (`postgres` and `qdrant` are Phase-3a-conditional. The `voice` template stays unchanged — gatekeeper moved into `oscar-household` instead.)
- `NousResearch/hermes-agent` — voice-gateway PR (the gatekeeper's Phase-0 pass-through path)
- Hermes Skills Hub — `smart-home/home-assistant` skill

Inside OSCAR proper, the open follow-ups are the speaker-ID enrolment wizard (Phase 2), the ingestion pipeline (Phase 3a), and the material-store encryption decision. Issues with reproductions or design suggestions are welcome at [github.com/mdopp/oscar/issues](https://github.com/mdopp/oscar/issues).

### Local dev setup

Before editing, install the pre-commit hook so CI's `pre-commit` workflow doesn't catch what should have been caught locally:

```bash
pip install --user pre-commit
pre-commit install
```

`.pre-commit-config.yaml` runs ruff (lint + format), trailing-whitespace, end-of-file-fixer, JSON/TOML/YAML validation. CI runs the same set — without the local install, dirty commits sail through and turn the next `pre-commit` workflow run red.

## License

[MIT](LICENSE). Same intent declared in every OSCAR-owned `pyproject.toml`.

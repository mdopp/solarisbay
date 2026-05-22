# Getting started — the fastest path to a working assistant

OSCAR is a thin household layer on top of [Hermes Agent](https://github.com/NousResearch/hermes-agent). Hermes on its own is `pip install`-able and **usable in minutes** — most of what OSCAR's five intents promise is reachable before any OSCAR-specific code or ServiceBay template is involved.

This page is the honest "what can I run today" walkthrough, in three tiers. Start at Tier 1; each tier is a superset of the one before.

> **Why this exists.** The architecture (`oscar-architecture.md`) describes the *end state*. This page describes the *entry point*. As of May 2026 Hermes ships native Home-Assistant control, a local document-retrieval skill (`qmd`), voice mode, Honcho memory with per-user peers, and 20+ messaging gateways — so Tiers 1 and 2 deliver real value with zero OSCAR code.

---

## Tier 1 — Hermes alone (≈ 5 minutes)

The point of Tier 1 is a **reality check**: is Hermes good enough on your hardware to be the load-bearing agent? Do this before investing in the OSCAR layer.

```bash
pip install hermes-agent
hermes postinstall
hermes model            # interactive — pick a provider (local Ollama or a cloud key)
hermes --tui            # start talking
```

That's a running agent. Ask it something, watch it use tools. For local-LLM mode, point `hermes model` at an Ollama endpoint; for a quick cloud trial, paste an Anthropic/OpenAI/OpenRouter key.

**What this validates:** conversation quality, latency on your box, whether local Ollama is fast enough, the general feel. If Hermes underwhelms here, that's a finding worth having *before* Sprint 5.

---

## Tier 2 — a real personal assistant (≈ 30 minutes, still zero OSCAR code)

Tier 2 layers on the capabilities that cover four of OSCAR's five intents — **without** ServiceBay templates, without `oscar-household`, without any merged OSCAR PR. It's Hermes' own features, configured.

### Home Assistant control (intent 5 — "things actually happen")

Hermes has a **native** HA integration — a gateway plus four device-control tools (`light`, `switch`, `climate`, `cover`, `media_player`, `fan`, `scene`, `script`, …). No HA-MCP server, no OSCAR skill.

```bash
export HASS_TOKEN=<home-assistant-long-lived-access-token>
export HASS_URL=http://<your-ha-host>:8123
hermes gateway run
```

Source: [Hermes HA integration](https://hermes-agent.nousresearch.com/docs/user-guide/messaging/homeassistant).

### Document & note memory (intent 2 — "long memory")

The `qmd` skill is a local hybrid-retrieval engine (BM25 + vector + LLM rerank) over a markdown knowledge base — no cloud.

```bash
hermes skills install official/research/qmd
```

Point it at a folder of markdown notes. Household "memory" — a book you read, a decision you made, a receipt's key facts — can start life as a markdown note that `qmd` indexes. (Whether OSCAR ever needs a *structured* books/records database on top of this is an open Phase-3a question — see `oscar-architecture.md`.)

Source: [qmd skill](https://hermes-agent.nousresearch.com/docs/user-guide/skills/optional/research/research-qmd).

### Chat on the road (intent 3 — chat side)

```bash
hermes gateway setup signal      # one-off: scan the QR with your phone's Signal app
```

Telegram, Discord, Slack, WhatsApp, Email and ~15 more work the same way.

### Voice (intent 3 — voice side, with a caveat)

Hermes' voice mode does local STT (faster-whisper, zero API keys) + TTS. Three shapes:

```bash
hermes
/voice on                        # CLI voice — mic on your computer
```

- **CLI voice** — talk to Hermes at a terminal.
- **Discord voice channels** — the bot joins a voice channel and holds a real-time hands-free conversation. **This is the closest thing to "voice in the house" that works today** — a family member talks to the assistant from a phone or tablet in a Discord voice channel.
- **Gateway voice replies** — spoken answers on Telegram/Discord.

**The caveat:** Hermes voice mode does **not** speak the Wyoming protocol and does **not** drive HA Voice PE hardware or multi-room satellites. A dedicated voice puck in each room — with speaker identification — is what OSCAR's `gatekeeper` adds in Phase 1/2. Until then, Discord-voice is the working Phase-0 voice interface.

### What Tier 2 gives you

| OSCAR intent | Tier-2 coverage |
|---|---|
| 1 — Sovereignty | Local LLM via Ollama; cloud opt-in. **Missing:** the per-call `cloud_audit` trail (OSCAR-eigen, Tier 3). |
| 2 — Long memory | Honcho (conversation) + `qmd` (documents/notes). **Missing:** structured domain collections — maybe never needed. |
| 3 — One conversation | Chat: full. Voice: CLI + Discord-voice. **Missing:** HA Voice PE pucks (gatekeeper, Phase 1). |
| 4 — Per-resident privacy | Honcho keeps per-user "peer" profiles. **Missing:** *voice = identity* — recognising who is speaking (gatekeeper speaker-ID, Phase 2). |
| 5 — Things happen | Full — native HA integration. |

Four of five intents, substantially, from `pip install` + config. That is the honest Phase-0 value, and it doesn't wait on anything.

---

## Tier 3 — the household deployment

Tier 3 is what the rest of this repo builds: OSCAR as a ServiceBay stack, for a household rather than one person.

What Tier 3 adds over Tier 2:

- **Packaged, wizard-driven deploy** — ServiceBay's `ai-stack` (Ollama + Hermes) plus OSCAR's `oscar-household` template, instead of a hand-run `pip install`.
- **Cloud-LLM audit** — every cloud call writes a `cloud_audit` row, family-readable via the `oscar-audit-query` skill.
- **HA Voice PE in the rooms** — the `gatekeeper` container bridges Wyoming-protocol voice pucks to Hermes (Phase 1).
- **Voice = identity** — speaker-ID maps a voice to an LLDAP resident and the right Honcho peer (Phase 2).
- **German-household defaults** and the per-resident harness composition.

Walkthrough: [`../stacks/oscar/README.md`](../stacks/oscar/README.md). It depends on ServiceBay's `ai-stack` templates ([mdopp/servicebay#544](https://github.com/mdopp/servicebay/pull/544)).

---

## Recommended sequence

1. **Do Tier 1 now** on the GPU server (or any box with Python). Confirm Hermes + local Ollama is fast enough.
2. **Do Tier 2** on the same box. Live with it for a few days — chat, HA control, `qmd`. Every finding (is `qmd` enough? are Honcho peers enough?) feeds back into the architecture and can *shrink* the OSCAR layer further.
3. **Move to Tier 3** when you want the household shape — multi-resident, audit, room voice pucks. By then the OSCAR-specific surface is well understood and small.

# hermes-webui

[Hermes Web UI](https://github.com/nesquena/hermes-webui) — a
Hermes-native chat surface for the household stack. Replaces the
Open WebUI template at the same `chat.<publicDomain>` URL (#1044).

## What it does

1. **Talks directly to Hermes' API server.** No OpenAI-compatibility
   shim, no second API key chain. Hermes' tool calls, memory layer
   (`memory.provider` per `templates/hermes/post-deploy.py`), Skills,
   Profiles, and Cron jobs all surface in the UI — they don't with
   Open WebUI because Open WebUI only sees the `/v1/models` /
   `/v1/chat/completions` slice.
2. **Reads from the same `~/.hermes` Hermes itself uses.** The pod
   mounts `${DATA_DIR}/hermes` at `/home/hermeswebui/.hermes/`, which
   is the same directory the `hermes` pod has at `/opt/data`. So when
   `hermes config set model.model gemma4:e4b` runs, hermes-webui's
   Models picker reflects it on next page load.
3. **Mounts the synced notes dir read-only at `/workspace`.** Hermes-WebUI's
   workspace browser surfaces the Obsidian vault for "link to a note in chat"
   flows. Read-only — hermes-webui's file-write controls don't touch the
   vault. The actual file write path is hermes' own skills / shell tools.

## Why we ship this instead of Open WebUI

| | Open WebUI | hermes-webui |
|---|---|---|
| Backend | OpenAI-shim `/v1/*` only | Native Hermes API |
| Sees Hermes Skills | No | Yes |
| Sees Hermes Sessions | Partial (re-implements its own) | Native |
| Sees Hermes Cron / Profiles | No | Yes |
| Model picker | OpenRouter / OpenAI catalogs | + Hermes' Models tab incl. local Ollama tags (#1053) |
| Container image footprint | ~3 GB | ~200 MB |
| Maintenance | Generic OpenAI client | Hermes-specific |

The Open WebUI template was a stop-gap (#1030) when the Hermes
dashboard URL `hermes.<publicDomain>` looked operator-flavored.
hermes-webui at `chat.<publicDomain>` is the right long-term shape.

## Variables

| Variable | Type | Purpose |
|---|---|---|
| `HERMES_WEBUI_PORT` | text | Host loopback port (default 8787). |
| `HERMES_WEBUI_PASSWORD` | secret | In-app password behind Authelia. Auto-generated. |
| `HERMES_WEBUI_SUBDOMAIN` | subdomain | `chat` by default. Internal exposure via NPM + Authelia. |

## Dependencies

- `hermes` (the agent loop hermes-webui talks to)
- `ollama` (Hermes' model backend)
- `nginx` (NPM proxy)
- `auth` (Authelia + LLDAP)

## First-run notes

- On first visit at `https://chat.<publicDomain>/`, Authelia challenges
  for 1FA login. Once authenticated, hermes-webui drops you into its
  onboarding wizard if Hermes hasn't been provisioned yet — for our
  install, `templates/hermes/post-deploy.py` has already done the
  provisioning, so the wizard short-circuits to the chat surface.
- The model dropdown surfaces Hermes' configured model + every entry
  under `custom_providers:` in `~/.hermes/config.yaml`. After #1053
  lands, that includes every local Ollama tag — no shell required to
  switch.
- The right-side workspace browser shows `/workspace` (= the synced
  notes dir read-only). Click a note → it goes into the chat context.

## Out of scope

- Sharing chat history with the upstream Hermes dashboard at
  `hermes.<publicDomain>`. The dashboard reads from the same
  `sessions/` dir so most of that is already true — but the two UIs
  manage their own per-page state independently. They're complementary,
  not interchangeable.
- Multi-user isolation. hermes-webui is single-tenant against
  `~/.hermes`. Multi-resident chat isolation is a Hermes-level
  feature (Honcho memory provider scopes per user); the UI is the
  same surface for everyone gated behind Authelia.

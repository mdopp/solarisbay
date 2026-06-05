# hermes-chat

A small, **stateless, offline-capable** household chat surface at
`chat.<publicDomain>`. Serves a static chat page from a tiny own image
(`ghcr.io/mdopp/oscar-chat`) and forwards turns to Hermes' **native session
API**. Replaces the fragile in-process `hermes-webui` (#139, #140).

The proxy source lives in `hermes-chat/` at the repo root (the image
context); this directory is the ServiceBay template that deploys it.

## What it does

1. **Serves a static chat page** — one current session, no CDNs (works
   offline). Calm, minimal look in the spirit of the ServiceBay / SolBay
   brand.
2. **Proxies to Hermes' native session API** over host loopback:
   `POST /api/sessions` to create the user's session, then
   `POST /api/sessions/{id}/chat` with `{"input": …}` per turn. (Not the
   gatekeeper's placeholder `/converse`.)
3. **Single sign-on (#134).** Behind Authelia forward-auth, NPM sets
   `Remote-User`; the proxy folds that into the Hermes `uid` — no second
   login. The bearer (`API_SERVER_KEY`) is held server-side, never in
   browser JS.

## Why this instead of hermes-webui

`nesquena/hermes-webui` ran the Hermes agent **in-process** and needed the
agent code importable in its own venv; the template only mounted Hermes'
data, so every message errored with `AIAgent not available`, and the
`:latest` pin drifted (#139). This pod has no in-process agent, no foreign
`:latest`, no own data store — it just talks to Hermes' API.

## Stateless / offline

- **No own data store.** All chat/session/memory lives in Hermes
  (`~/.hermes`). The browser keeps the current session id; the server keeps
  nothing.
- **Offline.** page → this proxy → Hermes (`127.0.0.1:{{HERMES_API_PORT}}`)
  → local Ollama. No external hop.

## Variables

| Variable | Type | Purpose |
|---|---|---|
| `HERMES_CHAT_IMAGE` | text | Image tag (default `ghcr.io/mdopp/oscar-chat:latest`). |
| `HERMES_CHAT_PORT` | text | Host loopback port (default 8787). |
| `HERMES_CHAT_SUBDOMAIN` | subdomain | `chat` by default. Internal exposure via NPM + Authelia. |
| `HERMES_API_PORT` | text | Hermes native API port (default 8642). |
| `HERMES_API_KEY` | secret | Bearer for Hermes; server-side only. |
| `DEFAULT_UID` | text | uid when the Authelia header is absent (offline test). |
| `TZ` | text | Time zone for log timestamps. |

## Dependencies

- `hermes` (the agent the proxy talks to)
- `ollama` (Hermes' model backend)
- `nginx` (NPM proxy)
- `auth` (Authelia + LLDAP)

## Migration

`post-deploy.py` decommissions the predecessors it replaces — `hermes-webui`
and, for older boxes, `open-webui` — when present: stops the pod, archives
any data dir under `${DATA_DIR}/_archived/`, and drops it from
`installedTemplates`. Idempotent: a fresh install finds neither and no-ops.

## Out of scope

- Session-list / new-session UI (#141) — this is one current session.
- The SolBay rename (#138) — naming stays on the current OSCAR scheme
  (`oscar-chat` image, `hermes-chat` template).

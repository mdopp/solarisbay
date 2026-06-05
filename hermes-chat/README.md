# solilos-chat

A small, stateless, offline-capable household chat proxy. Serves a static
chat page and forwards turns to Hermes' **native session API**. Replaces
the fragile in-process `hermes-webui` (#139).

## Design

- **Stateless** — holds no chat/session store. The browser keeps the
  current session id; all chat/session/memory state lives in Hermes
  (`~/.hermes`). No duplication, no ambiguity about where data is.
- **Offline** — page → this proxy → Hermes API
  (`127.0.0.1:{{HERMES_API_PORT}}`/8642) → local Ollama. No external hop;
  the page bundles its own CSS/JS (no CDNs).
- **Not fragile** — own tiny image, no foreign `:latest`, no in-process
  agent.

## SSO (folds in #134)

Behind Authelia forward-auth, NPM sets `Remote-User` after the user
authenticates. This proxy reads that header and folds it into the Hermes
`uid` — no second login. The bearer token (`API_SERVER_KEY`) is held
server-side and never reaches the browser. The pod binds loopback, so only
NPM (which sets the header after Authelia) can reach it.

Absent header (e.g. direct loopback access for offline testing) falls back
to `DEFAULT_UID`.

## Hermes contract

- `GET /api/sessions?user_id={uid}` — list the uid's sessions.
- `POST /api/sessions` — create a session bound to the uid.
- `GET /api/sessions/{id}` — get a session + its message history.
- `POST /api/sessions/{id}/chat` — body `{"input": …}`, returns the reply.

(Not the gatekeeper's placeholder `/converse`, which does not exist in the
real Hermes API.)

## Per-user privacy

Every session is created with `user_id: uid` (the SSO identity). The proxy
scopes both the list and single-session fetch to the caller's uid — it
passes `user_id` to Hermes **and** re-filters by each session's own
`user_id`, so a resident sees only their own sessions and cannot open
another resident's session by guessing its id (returns 404).

## Environment

| Var | Default | Purpose |
|---|---|---|
| `CHAT_HOST` | `127.0.0.1` | Loopback bind for NPM. |
| `CHAT_PORT` | `8787` | Host loopback port. |
| `HERMES_URL` | `http://127.0.0.1:8642` | Hermes native API base. |
| `API_SERVER_KEY` | — | Bearer for Hermes; server-side only. |
| `REMOTE_USER_HEADER` | `Remote-User` | Authelia identity header → uid. |
| `DEFAULT_UID` | `household` | uid when the header is absent. |

## Endpoints

- `GET /` — the chat page.
- `GET /health` — `{"ok": true}`.
- `GET /api/sessions` — `{"ok": true, "sessions": [{id, title, last_activity}]}`
  (the caller's own sessions only).
- `POST /api/sessions` — `{"ok": true, "session_id": …}` (new session for the uid).
- `GET /api/sessions/{id}` — `{"ok": true, "session": {id, title, last_activity, messages}}`
  or `404` if it isn't the caller's session.
- `POST /api/chat` — `{"input": …, "session_id": …?}` →
  `{"ok": true, "session_id": …, "reply": …}`.

## Run

```
pip install -e .
HERMES_URL=http://127.0.0.1:8642 API_SERVER_KEY=… python -m solilos_chat
```

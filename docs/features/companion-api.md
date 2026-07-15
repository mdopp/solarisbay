# Solaris Companion API — the contract the `solaris-android` app talks to

This is the authoritative reference for the **companion-facing surface** of the
Solaris engine (`solaris-chat`): the `/napi/*` native API, device-token auth, the
live event stream, and Web Push. The Android companion app (repo
**`mdopp/solaris-android`**) targets only this surface — it never talks to
ServiceBay or Home Assistant directly; Solaris aggregates those behind `/napi`.

> **Source of truth is the code, not this file.** This doc is generated from
> `solaris-chat/src/solaris_chat/{server.py,engine/sb_companion.py,engine/sb_events.py,engine/notify.py,push_store.py,device_token_store.py,config.py,static/sw.js}`
> as of **v0.25.1**. If a detail here disagrees with the running engine, the code
> wins — file an issue. Paths are stable; response shapes may gain fields.

## Base URL, transport, auth surfaces

- **Host:** the Solaris engine (prod: `https://chat.dopp.cloud`; LAN IP works too). HTTPS required.
- **Two auth surfaces:**
  - **`/napi/*` — native, device-token only, proxy-bypassed (Authelia is skipped).**
    Every route is **fail-closed**: a missing/invalid `sol_device_` bearer ⇒ **401**.
    It never falls back to `default_uid` and never trusts the `Remote-User` header.
  - **`/api/*` — browser/PWA, Authelia-gated** (forward-auth `Remote-User`). The app
    uses a couple of these for pairing and Web Push (called out below); everything
    else the app needs is under `/napi`.
- **No API version in the path.** Capability is signalled in responses (e.g.
  `vapid_public_key` in `whoami` ⇒ push available).

## 1. Device-token auth (how the app authenticates)

- **Token format:** `sol_device_<urlsafe-32-bytes>` — plaintext shown **once** at pairing.
  Stored server-side only as a SHA-256 digest; compared constant-time; per-resident (`owner_uid`).
- **Pairing (mint a token):** `GET/POST /pair-device` — **Authelia-gated** (interactive login).
  `POST /pair-device` (form field `label`) mints a token and **302-redirects to
  `<android_package>://pair#token=<plaintext>&id=<id>`** — the token rides the URL
  **fragment** so it never hits server logs. The app captures it from the deep link.
- **Use:** send `Authorization: Bearer sol_device_...` on every `/napi/*` call.
- **Manage devices (device-token authed):**
  - `GET /napi/device-tokens` → `{ok, tokens:[{id,label,created,last_used}]}` (metadata only)
  - `DELETE /napi/device-tokens/{id}` → `{ok}` (owner-checked; **404** if not yours)

## 2. `/napi/*` endpoints

All require `Authorization: Bearer sol_device_...`. Common errors: **401** (no/bad token),
**502** (upstream HA/SB unavailable), **503** (upstream unconfigured).

### Home / portal (Home-Assistant-backed)
| Method | Path | Returns |
|---|---|---|
| GET | `/napi/whoami` | `{ok,uid,is_admin,version,logout_url,context_window,household_session_id,wartung_session_id,vapid_public_key}` |
| GET | `/napi/portal/start` | `{ok,personal:[…],household:[…],ha}` — pinned favorites enriched with live HA state |
| GET | `/napi/portal/start/addable` | `{ok,rooms:[{room,cards:[…]}],automations:[…]}` — picker of addable actuators |
| GET | `/napi/portal/active` | `{ok,active:[{entity_id,name,room,domain,state}]}` — currently on/open |
| GET | `/napi/portal/cameras` | `{ok,cameras:[{entity_id,name,room}]}` |
| GET | `/napi/portal/state?entity_id=<id>` | `{ok,card:{…}}` — one entity's card spec (**400** on bad id) |
| GET | `/napi/portal/energy` | `{ok,energy:{…}}` |
| GET | `/napi/portal/entity-history?entity_id=<id>&range=<24h\|48h\|7d>` | `{ok,history:[…]}` (**400** on bad id/range) |
| GET | `/napi/portal/camera/{entity_id}/snapshot` | raw image bytes (**400** if not a camera) |
| POST | `/napi/portal/watch` | body `{entity_ids:[…]}` → `{ok}` — set this device's widget watch-set (**503** if unavailable) |

### HA actions
| Method | Path | Body | Notes |
|---|---|---|---|
| POST | `/napi/ha/call` | `{entity_id,service,data?,confirmed?}` | domains `light\|switch\|cover\|climate`. Sensitive covers (garage/door/gate open) need `confirmed:true` (**403** otherwise). **400** bad domain. |

### ServiceBay BFF (Solaris aggregates ServiceBay — ADR 0010 / #811)
| Method | Path | Body | Returns |
|---|---|---|---|
| GET | `/napi/servicebay/{key}` | — | SB JSON verbatim. `key ∈ {home,approvals,services,upgrades}` (**404** unknown key) |
| POST | `/napi/servicebay/services/{name}/operate` | `{action:"start"\|"stop"\|"restart"}` | `{ok,name,action}` (**400** bad action) |

> Upgrade-apply is intentionally **not** on `/napi` (it needs a mutate-scoped SB token =
> standing broad credential, which Solaris does not hold). Show "upgrade available"
> from `/napi/servicebay/upgrades`; the admin applies it in the ServiceBay web UI.

### Uploads (camera / documents)
| Method | Path | Content-Type | Limits | Returns |
|---|---|---|---|---|
| POST | `/napi/upload` | `multipart/form-data` (`file` repeatable, opt. `filename`,`kind`) | JPEG/PDF only (**415** else), ≤25 MB (**413**), ≤20 files (**400**) | one: `{ok,id,url}` · many: `{ok,files:[{ok,id,url}]}` |

Stored in the notes vault (`users/<uid>/uploads/`, household → shared `uploads/`) as a
companion note with an Obsidian embed — so uploads are also searchable via notes_search.

## 3. Live events — SSE (`/napi/portal/events`)

- `GET /napi/portal/events` — device-token authed, `Content-Type: text/event-stream`,
  long-lived (socket-connect timeout 20s, **no** read timeout; reconnect on drop).
- One multiplexed stream per resident (`uid`), in-process pub/sub (`EventBus`), per-resident privacy.
- **Event kinds** (`event: <kind>` + `data: <json>`):
  - `card_state` — an HA entity changed (fan-out from the HA watcher). Update the card in place.
  - `chat` — a backgrounded/finished chat turn or a server-injected card.
  - `servicebay` — a ServiceBay **approval** event, republished from SB's SSE:
    `data:{id,kind,summary}`. Show an approval card. (Verdict flow below.)

**Approval verdict** is *not* on `/napi`: the admin deep-links to
`/api/servicebay/approvals/{id}/{approve|reject}` (Authelia-gated), which mints an
ephemeral delegated-admin assertion from the live session. The companion surfaces the
approval (from the SSE `servicebay` event) and hands off to that authed web action.

## 4. Web Push (VAPID) — background notifications

- **VAPID public key:** returned as `vapid_public_key` in `/napi/whoami` (and `/api/whoami`).
  Use it as `applicationServerKey` when subscribing.
- **Subscribe / unsubscribe** — the native app uses the **device-token** `/napi` twins
  (owner-scoped; same body/semantics as the browser `/api` pair):
  - `POST /napi/push/subscribe` body `{endpoint,keys:{p256dh,auth}}` → `{ok}` (401 without a `sol_device_` token)
  - `POST /napi/push/unsubscribe` body `{endpoint}` → `{ok}`
  - `endpoint` is any HTTPS URL — for the native app, your **UnifiedPush distributor**
    endpoint (self-hosted ntfy etc.); the server POSTs standard RFC8291/VAPID Web Push there.
  - Browser PWA equivalents (Authelia-gated): `POST /api/push/subscribe` / `/api/push/unsubscribe`.
- **Selective push:** the server only sends a Web Push when the app is **backgrounded**
  (no open SSE subscriber for that uid). If the app is foregrounded, it gets the event
  live over SSE — no redundant push.
- **Payload:** `{title, body(≤140 chars), data:{kind:"chat"|"reminder"|"card_state"|"servicebay", session_id?, url?, timer_id?, id?}}`.
  `servicebay` (approval) events are pushed too when backgrounded: `data:{kind:"servicebay", id, url}`.
  Service worker (`/sw.js`) shows the notification; `notificationclick` deep-links to
  `data.url` (e.g. `/#/c/<session>`), else app root. `tag` collapses per session/timer.

## 5. Android app-domain binding (TWA)

- `GET /api/.well-known/assetlinks.json` serves Digital Asset Links for the package
  `ANDROID_PACKAGE` (default `cloud.dopp.solaris`) using `ANDROID_CERT_FINGERPRINTS`
  (set at signing). This removes the URL bar in a Trusted Web Activity and binds app↔domain.

## What the companion does **not** touch

- **ServiceBay / Home Assistant directly** — always via `/napi`.
- **The durable SB read token / `SB_READ_TOKEN`** — server-internal only (powers the
  approval SSE bridge + update poller); the companion is unaffected and only ever uses
  its `sol_device_` token. (See the engine's `reference_event_driven_read_token`.)

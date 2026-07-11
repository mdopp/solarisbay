# Solaris on the phone — Web Push, live status propagation, Android app

> **Status:** planned (2026-07-11). Concept + phasing for turning the Solaris web
> app into a phone experience with real OS integration. Tracked by the *Solaris
> mobile* epic. Phase 1 is buildable in this repo today; Phases 2–3 are a separate
> Android sub-project.

## Goal

Make Solaris a first-class phone app — **without rewriting the UI in Kotlin**.
"Essentially this program" plus native OS surfaces:

- **Notifications** — reminders/timers/alarms (and noteworthy events) arrive as
  phone notifications, not only on the Home-Assistant speaker.
- **Live status everywhere** — the favorites cards (garage open/closed, light
  on/off …) and chat turns propagate their current state to every surface in
  real time, through **one** infrastructure.
- **Home-screen widgets + status-bar/quick-settings** — cards as widgets and a
  chat entry point on the home screen.

## Three technical truths that shape the design

1. **Home-screen widgets and quick-settings tiles are unavoidably native** on
   Android (`AppWidgetProvider`/`RemoteViews`, `TileService`) — they cannot be
   built in web tech. So "no Kotlin" can't be fully kept; the native part is
   **minimised** — only the OS surfaces are native, the whole chat/cards/energy/
   notes UI stays the web app inside a thin shell.
2. **Web Push (VAPID) needs no Google/FCM** and works **in the installed PWA
   with no native app**. So the headline feature (notifications) is pure server +
   PWA work, and it carries over unchanged when a TWA later wraps the PWA.
3. **Web Push is for events, not high-frequency silent state updates** (Android/
   Chrome throttle silent pushes and want a visible notification). Live card
   status is therefore split by app state: **app open → SSE** (instant, replaces
   the 12 s poll from #711); **app closed / widget → Web Push only for noteworthy
   events** + refresh on open (widgets get instant native updates in Phase 3).

And: **Play Store is an evolution of sideload, not a rebuild** — the same app
project ships as a sideloadable APK *and* a Play AAB; the extra cost is packaging/
admin (Play Console one-time fee, an upload signing key + Play App Signing, an AAB
instead of a debug APK, a store listing, review — private use can go through the
Internal/Closed testing tracks without public review).

## Decisions

- **Push transport: Web Push / VAPID** (no FCM/Google).
- **v1 focus: notifications first** (reminders/timers to the phone) — mostly
  server + PWA.
- **Distribution: Play Store**, reached as an evolution of a sideloadable build.
- **One channel propagates card status *and* chat** (see Phase 1b/1c).

---

## Phase 1 — Notify/Event backbone + Web Push (this repo, PWA)

Today the only delivery point is `TimerScheduler._announce`
(`engine/scheduler.py`), hardwired to HA `assist_satellite.announce` (speaker
TTS). Phase 1 introduces a **typed event bus + notify fan-out** and keeps the
speaker channel.

### 1a — Web Push for timers/reminders (the core)
- **Service worker** `static/sw.js` — minimal, `push` + `notificationclick`
  only, **no caching** (respects #648's no-SW decision). Served at **root scope**
  via a dedicated `/sw.js` route (`Service-Worker-Allowed: /`, `no-cache`) — not
  under `/static/`, or the scope would be wrong. Registered from `index.html`.
- **VAPID keys via env** (no secrets in repo; mirror `SOLARIS_API_KEY`):
  `VAPID_PUBLIC_KEY/PRIVATE_KEY/SUBJECT` in `config.py`; the public key rides
  `/api/whoami` to the client (no extra round-trip); library `pywebpush` added to
  `pyproject.toml`.
- **Subscription store** — migration `0020_push_subscriptions`
  (`push_subscriptions(id, owner_uid, endpoint UNIQUE, p256dh, auth, user_agent,
  created, last_ok)`), store module `push_store.py` mirroring `favorites_store.py`
  (upsert-by-endpoint, list-for-uid, remove-by-endpoint, mark-ok, degrade-to-empty).
- **Endpoints** (Authelia-gated, owner-scoped): `POST /api/push/subscribe`
  (`PushSubscription.toJSON()`), `POST /api/push/unsubscribe`. Frontend does the
  permission prompt + `pushManager.subscribe` + POST, and re-subscribes on
  endpoint expiry.
- **Notifier** `engine/notify.py` — `async push(uid, title, body, data)` sends to
  every subscription of the uid via `pywebpush` in a thread; **prunes on 404/410**;
  errors logged, **never breaks the timer loop**; no-op when VAPID is unset.
- **Wiring** — `TimerScheduler` gets an optional `notifier`; `_fire_due` calls
  `notifier.push(...)` in addition to the speaker announce (speaker stays the
  primary channel; push is best-effort).

### 1b — Live event bus + SSE + HA state subscription (status propagation)
- `notify.py` becomes an **event bus** with typed events `reminder · card_state ·
  chat` (in-process asyncio pub/sub, keyed by uid).
- **HA state source** — a new HA **WebSocket** subscriber (`subscribe_entities`/
  `state_changed`) over the union of all residents' pinned entities. On a change,
  emit `card_state` to the uids that pinned it. Conceptually this replaces the
  12 s poll from #711 with push.
- **SSE `/api/events`** (Authelia-gated, owner-scoped, like the existing
  `/api/sessions/{id}/events`) — an open client updates the single card in place
  instantly; `/p/start` uses SSE (poll stays as fallback).
- **Web Push for `card_state` is selective** — only noteworthy transitions
  (cover/door open-close, security) push a notification while the app is closed;
  lights/dimmers propagate over SSE only.

### 1c — Chat propagation
- New chat turns (voice/background answers, long "Gründlich" replies) emit a
  `chat` event: **SSE when the app is open**, **Web Push notification when
  backgrounded** (deep-link to the originating session). A "foregrounded?"
  heuristic avoids double-notifying.

---

## Phase 2 — TWA on the Play Store (new Android sub-project)

- A **Trusted Web Activity** (Bubblewrap/PWABuilder) wraps the PWA
  (`chat.dopp.cloud`) — the thinnest wrapper, real Chrome engine, so **Web Push +
  SSE work unchanged inside it** (this is why a TWA fits Web Push better than a
  Capacitor/WebView shell).
- **Digital Asset Links** — serve `/.well-known/assetlinks.json` from
  solaris-chat/nginx to verify the app↔domain binding (removes the URL bar).
- **Auth** — the TWA shares Chrome Custom Tab cookies, so the Authelia login works
  as in the browser.
- **Sideload → Play is an evolution** — same project, just a signed AAB + Play
  Console + listing; the sideload APK stays for quick family installs.
- Lives in a new `android/` sub-project (or separate repo), **not** mixed into
  solaris-chat.

## Phase 3 — Native surfaces (Kotlin, fast-follow, in the TWA app project)

- **Device token / app password** (server, this repo) — an endpoint that mints a
  long-lived scoped `Authorization: Bearer sol_device_…` from an authenticated
  Authelia session (mirrors the ServiceBay token pattern); the API accepts it as
  an alternative to the Authelia headers and resolves to the same uid. This is the
  **prerequisite** for anything native (widgets/services can't ride the browser
  session).
- **Home-screen card widget(s)** (`AppWidgetProvider` + `RemoteViews`) — render
  cards from `/api/portal/start?state_only=1`, act via `/api/ha/call` (with the
  device token), refresh via `WorkManager` + an optional push "nudge" for instant.
- **Chat-entry widget** (deep-link into the app).
- **Optional** — a quick-settings `TileService` + an ongoing status-bar notification.

## Defaults (change on request)

1. **Subscribe toggle** = a bell in the header, shown only when VAPID is set and
   permission isn't granted yet.
2. **Alarm sound on the phone** = a notification with the system sound; the loud
   alarm stays the HA speaker.
3. **Noteworthy `card_state` push** = covers (garage/door) open/close + security;
   lights/dimmers over SSE only, no push.
4. **Chat push (1c)** = only when the app is backgrounded and the turn wasn't
   started by the active client.

## How to verify (Phase 1, on the box)

VAPID env set → install the PWA → grant permission → `/api/push/subscribe` writes
a row → set a timer → **a notification arrives on the phone AND the speaker
rings**; flip a real light/garage → an open `/p/start` updates the card
**instantly** (SSE), and a *cover* change while backgrounded pushes a
notification; the `pywebpush` dep is in the image; a 410 endpoint prunes the
subscription.

## Related

- Architecture: [`../../solaris-architecture.md`](../../solaris-architecture.md).
- Favorites/live cards: [`favorites-start-page.md`](favorites-start-page.md) (the
  `state_only=1` refresh from #711 that SSE upgrades).
- Chat/voice + the scheduler that fires reminders:
  [`chat-and-voice.md`](chat-and-voice.md).

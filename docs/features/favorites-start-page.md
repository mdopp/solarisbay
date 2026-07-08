# Favorites / start page

`/p/start` is your personal home screen for the house: the device cards and
action buttons you use most, pinned so a routine action is one tap instead of a
voice round-trip. Say „packe das Bürolicht auf meine Startseite" in chat or by
voice and the card is there.

Design rationale: [`solaris-concept.md`](../solaris-concept.md) §2.

<!-- screenshot: /p/start -->

## What it does

- **Three sections**: **Meine Favoriten** (personal), **Haushalt** (shared),
  **Häufig genutzt** (the actions you run most, offered automatically).
- **Cards are the chat cards, made permanent.** The start page reuses the chat
  card renderers 1:1 (`renderHaCard`) — device cards show live HA state, action
  buttons run the exact tool call that was pinned, link cards navigate.
- **One URL for everyone.** `https://<host>/p/start` sits behind the same
  Authelia SSO; the `Remote-User` header selects *your* personal section.
  Bookmark it, add it to the home screen — no per-user paths.

## How to use it

### Pin by voice or chat

- „packe das Bürolicht auf meine Startseite" → pins that device card.
- „pack das auf meine Startseite" right after an action → pins **the last
  action that just ran**, with its exact arguments.
- „pack das auf unsere Startseite" → the shared **Haushalt** page.
- „nimm X von meiner Startseite" → removes it.

An identified resident pins personally; the anonymous voice uid pins to the
shared page.

### Pin by tap

Every card in the chat log has a small ☆ affordance that pins the same payload.

### Add via the card picker

On the start page, tap **Bearbeiten** → **+ Karten hinzufügen**. A room-grouped
overlay lists every addable device (already-pinned ones marked), plus your
automations. Select several and add them at once, to your personal or the
household page.

### Häufig genutzt

Actions you run repeatedly show up automatically as ordinary cards. One tap on
☆ promotes one to a permanent favorite. Frequency never silently reshuffles
your curated sections.

## Safety

Confirm-gated actions (locks, garage/gate covers — `engine/confirm.py`) are
**refused at pin time** and the card picker marks them `sensitive`. Defense in
depth: `POST /api/favorites/{id}/run` re-checks the same policy server-side and
returns 403 — a one-tap tile can never bypass the confirmation gate.

## How it works (brief)

- The pin tool is `pin_favorite(target?, scope?, remove?)`
  (`engine/tools/favorites.py`). The **handler decides deterministically**
  (code-side steering beats prompting the small model): a resolvable `target`
  → entity card; no target → the session's most recent side-effectful call from
  the in-process trace recorder, filtered to the pinnable allowlist
  (`ha_call_service`, `play_music`, `play_radio`, `media_find_podcast`, …) →
  action card with the exact args. The model never reconstructs arguments.
- **Routes** (`server.py`): `GET /api/portal/start` returns
  `{personal, household, frequent}` (entity cards enriched with live HA state);
  `GET /api/portal/start/addable` is the room-grouped picker source;
  `POST /api/favorites`, `PUT /api/favorites/{id}` (reorder),
  `DELETE /api/favorites/{id}`, `POST /api/favorites/{id}/run`. The page itself
  is served by the SPA shell at `/p/{type}`.
- **Storage**: `favorites` and `favorite_usage` tables in `solaris.db`
  (owner-scoped; `owner_uid = 'household'` is the shared page), Alembic
  migration `0019_favorites`. Favorites are operational state, so they live in
  the db, not the vault.

## Related: playlists & PWA

- **Playlists are not favorites.** „Füge das meiner Playlist hinzu" writes a
  real Jellyfin playlist (media content has order/playback semantics Jellyfin
  owns). The playlist itself is pinnable as an ordinary `action` favorite.
- **Mobile / PWA**: on phones the header becomes a bottom tab bar — 🏠 Zuhause ·
  💬 Chats · ⭐ Favoriten (`#/p/start`). `manifest.json` +
  `apple-mobile-web-app-capable` make it installable to the home screen as a
  standalone app.

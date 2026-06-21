---
name: solaris-media
description: Play and control household media — Jellyfin music/audiobooks/movies, live internet radio, and podcasts — on a room speaker or Cast device via Home Assistant media_player. Use for "play …", "next/previous", "pause/resume", "play <radio station>", "play the latest <podcast>".
kind: skill
scope: household
version: 1.0.0
author: Solaris
license: MIT
---

# Solaris — Media (Jellyfin & internet radio)

Play and control household media on a target room device. Two sources:

- **Jellyfin** — the house media server (Music / Movies / Shows libraries +
  Audiobooks via the Bookshelf plugin). Wired into HA as the `jellyfin`
  integration, so its library is reachable through `media_player.play_media`.
- **Live internet radio** — not in the Jellyfin library; played by a station
  name (HA Radio Browser) or a direct stream URL.
- **Podcasts** — found via the free, keyless fyyd.de index by show name; the
  newest episode is resolved and played on the room device.

## When to use

- "Spiel <Künstler/Album/Playlist> im Wohnzimmer."
- "Leg mein Hörbuch wieder auf." / "Spiel den Film X im Wohnzimmer."
- "Weiter." / "Zurück." / "Pause." / "Mach weiter." / "Stopp."
- "Spiel Radio <Sender>." / "Spiel den Stream <URL>."
- "Spiel die neueste Folge von <Podcast>." / "Leg den <Podcast> auf."

Out of scope: ingesting an uploaded image/audio file
(`media-ingestion-multimodal`), buying/managing devices (HA setup).

## Target the right device

Resolve the room to a `media_player.*` entity from the device registry (match
the room/Name in the steerable-device list). If the request names no room and
only one media player exists, use it; otherwise ask which room.

## Play from Jellyfin

Call `ha_call_service media_player.play_media` on the target entity:

- `media_content_type`: `music` (artist/album/track), `tvshow` / `movie` for
  video, or `music` for an audiobook track. For a whole playlist/album use the
  library's playlist/album item.
- `media_content_id`: the Jellyfin item the HA integration exposes for the
  match. Resolve the request to a library item first (by artist / album / title
  / show); play the resolved item, not a guessed id.

If the search is ambiguous (several artists/albums match), name the top matches
and ask which one rather than guessing.

## Play internet radio

A station name or a stream URL → `media_player.play_media`:

- `media_content_type`: `music`
- `media_content_id`: the resolved stream URL. For a named station, resolve it
  via HA Radio Browser; for a direct URL the request already gives the id.

## Play a podcast

Call `media_find_podcast` with the show `name` and the room's `entity_id`
(a `media_player.*`). It resolves the show on fyyd.de, picks the newest
episode's audio, and plays it via `media_player.play_media`. If no room was
named, call it with `name` only first — it returns the resolved episode without
playing; then ask which room and call again with the `entity_id`.

- "Spiel die neueste Folge von Lage der Nation im Wohnzimmer" →
  `media_find_podcast {name: "Lage der Nation", entity_id: "media_player.wohnzimmer"}`.
- Show not found / no episode → say so plainly; don't guess a feed URL.

## Transport control

Once something is playing, control it on the same entity:

- next → `media_player.media_next_track`
- previous → `media_player.media_previous_track`
- pause → `media_player.media_pause`
- resume / play → `media_player.media_play`
- stop → `media_player.media_stop`
- volume → `media_player.volume_set` (`volume_level` 0.0–1.0)

Act without confirmation — media is low-risk. Switching stations is just a new
`play_media` on the same device.

## Failure paths

- No matching library item / station → say so and offer the closest matches.
- The target device is unavailable → name the device and suggest another room.

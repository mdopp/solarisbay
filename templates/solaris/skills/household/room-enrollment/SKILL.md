---
name: solaris-room-enrollment
description: When a room-dependent voice command arrives from a satellite with no known room (or a resident says "this is the <room>"), ask/persist the satellite→room mapping, then proceed.
kind: hook
scope: household
event: missing-room
version: 2.0.0
author: Solaris
license: MIT
---

# Solaris — Room Enrollment

**Binds:** `missing-room` (a voice satellite with no room mapping yet gets a
room-dependent command, or a resident explicitly (re)assigns the satellite's room)

Voice-PE pucks don't know which room they're in. Every voice turn carries
`endpoint` (`voice-pe:<satellite_id>`) and `location` (the mapped room, or
null/empty when unenrolled). The mapping lives in `solaris.db` and is written
**only** through the gatekeeper `set_room` MCP tool — the agent never holds the
push credential. Text/chat turns have no satellite; this never applies there.

## What to do on the event

### Unknown-room gate
The command needs a room ("mach das Licht an", "stell die Heizung hier auf 21°")
and `location` is null/empty:

1. Don't run the action yet. Ask once: *"In welchem Raum sind wir gerade?"* /
   *"Which room are we in?"*
2. Extract the room from the answer and call `set_room` with this turn's endpoint:
   ```json
   {"endpoint": "voice-pe:<satellite_id>", "room": "<room>"}
   ```
3. On `{"ok": true}` confirm (*"Alles klar, wir sind in der Küche."*) then **carry
   out the original action**. On `{"ok": false, "reason": …}` don't loop — say you
   couldn't save the room and skip the action gracefully.

### Explicit (re)mapping
The resident says "das hier ist das `<Bad>`" / "we're in the `<room>`" at any time:
call `set_room` with the current endpoint + new room (insert-or-overwrite),
confirm (*"Notiert — dieser Raum ist jetzt das Bad."*), then run any chained
action.

### Reading the mapping
If asked "welcher Raum bin ich gerade?", call `list_rooms` (read-only). Answer with
the room name; never read raw satellite IDs aloud.

## Guards

- **Ask at most once per turn**; if the resident declines, drop the action.
- **One write per answer**: call `set_room` once, don't retry `ok:false` in a loop.
- **Never invent a room**: only persist what the resident actually said.

## Failure paths

- `invalid_room` / `invalid_satellite_id` → ask the resident to repeat the room
  once, then give up.
- `db_not_ready` → say you can't save the room right now and skip the action.
- `set_room` / `list_rooms` unreachable (gatekeeper-mcp down) → "Ich weiß gerade
  nicht, in welchem Raum wir sind" and stop.

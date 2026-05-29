---
name: oscar-room-enrollment
description: Use when a resident gives a room-dependent voice command (turn on the light, set the temperature here, what's the time in this room) but OSCAR does not know which room the satellite is in, OR when a resident says "this is the <room>" / "we're in the <kitchen>" / "das hier ist das Bad" to (re)assign the current satellite to a room. Persists the satellite->room mapping via the gatekeeper-mcp `set_room` tool, confirms, then proceeds with the original action. Voice only — irrelevant to text/chat sessions.
version: 1.0.0
author: OSCAR
license: MIT
---

# OSCAR — Room Enrollment

## Overview

Voice satellites (Voice-PE pucks) don't inherently know which room they're in.
The gatekeeper tags every voice turn with the originating satellite and its
known room, but a freshly-placed satellite has **no room** yet. When a resident
asks for something room-dependent from such a satellite ("turn on the light"),
OSCAR can't resolve "the light" — so it asks once, remembers the answer, and
carries on.

The mapping lives in `oscar.db` and is written through the **`set_room` MCP
tool** exposed by the gatekeeper (the `gatekeeper-mcp` server). The agent never
holds the gatekeeper push credential — room writes go through this tool only.

## Session context the gatekeeper provides

Each **voice** turn carries two fields from the gatekeeper:

- `endpoint` — `voice-pe:<satellite_id>`, identifying the puck the resident is
  speaking to.
- `location` — the room that satellite is currently mapped to, or **null/empty**
  when the satellite has never been enrolled.

A text/chat turn has no satellite, so this skill does not apply there.

## When to use

Trigger in exactly two situations:

1. **Unknown-room gate.** The resident gives a command that *needs* a room
   (a device or scene scoped to "here": "mach das Licht an", "turn on the
   light", "stell die Heizung hier auf 21°", "is the window here open?") **and**
   `location` is null/empty. Ask for the room, persist it, then proceed.

2. **Explicit (re)mapping.** The resident says, at any time, "this is the
   `<room>`", "we're in the `<room>`", "das hier ist das `<Bad>`", "hier ist
   die `<Küche>`". Remap the current satellite to that room — even if it was
   already mapped (residents move pucks around).

**Do not** trigger when:
- `location` is already set and the resident isn't remapping — just do the action.
- The command is room-independent ("what time is it?", "schreib eine Notiz",
  "wie ist das Wetter?"). Room is irrelevant; never ask.
- It's a text/chat session (no satellite to enrol).

## Operating sequence

### Unknown-room gate (situation 1)

1. Recognise the command needs a room and `location` is null/empty. Don't run
   the action yet.
2. Ask, briefly and in the household language:
   *"In welchem Raum sind wir gerade?"* / *"Which room are we in?"*
3. On the resident's answer, extract the room name (e.g. "Küche", "kitchen",
   "Bad"). Call the `set_room` tool with the current satellite:
   ```json
   {"endpoint": "voice-pe:<satellite_id>", "room": "<room>"}
   ```
   (Pass the `endpoint` value from this turn's session context verbatim; the
   tool strips the `voice-pe:` prefix itself. You may pass `satellite_id`
   directly instead if you have it.)
4. Check the tool result:
   - `{"ok": true, ...}` → confirm: *"Alles klar, wir sind in der Küche — sag
     jederzeit 'das hier ist <Raum>', wenn ich das ändern soll."* Then **carry
     out the original action** the resident asked for.
   - `{"ok": false, "reason": "..."}` → don't loop. Tell the resident you
     couldn't save the room right now and skip the action gracefully.

### Explicit (re)mapping (situation 2)

1. Extract the target room from "this is the `<room>`".
2. Call `set_room` with the current `endpoint` and the new room (same call as
   above). This inserts or overwrites the mapping.
3. Confirm: *"Notiert — dieser Raum ist jetzt das Bad."*
4. If the resident chained an action ("this is the bath, turn on the light"),
   proceed with it after the confirmation; otherwise just confirm.

## Reading the current mapping

If you need to know which satellites are mapped where (e.g. the resident asks
"welcher Raum bin ich gerade?"), call `list_rooms` — it returns
`{"rooms": {"<satellite_id>": "<room>"}}`. Read-only; don't read raw
satellite IDs aloud, answer with the room.

## Guards

- **Ask at most once per turn.** If the resident declines to name a room, drop
  the action — don't badger.
- **One write per answer.** Call `set_room` once with the resident's answer;
  don't retry on `ok:false` in a loop.
- **Never invent a room.** Only persist a room the resident actually said.
- **Stay verbal-light.** This is a voice flow; keep the confirmation to one
  short sentence.

## Failure paths

- `set_room` returns `invalid_room` / `invalid_satellite_id` → the parse went
  wrong; ask the resident to repeat the room name once, then give up if it
  fails again.
- `set_room` returns `db_not_ready` → the room store isn't available; tell the
  resident you can't save the room right now and skip the action.
- `set_room`/`list_rooms` tool not reachable → the gatekeeper-mcp server is down;
  fall back to "Ich weiß gerade nicht, in welchem Raum wir sind" and stop.

## Related

- `#91` / `oscar.db` `voice_pe_rooms` table — the data plane this writes to.
- The gatekeeper `set_room` / `list_rooms` MCP tools (`gatekeeper-mcp`) —
  the only authenticated path from Hermes to the room store.
- Longer term the room source of truth should be Home Assistant (device→area);
  this skill writes the interim `oscar.db` store. See `#94`.

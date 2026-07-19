# ADR 0007 — Frontend: no new surface; converge on one inbox

**Status:** Accepted

## Context

The chat UI is already at its natural limit: 5 rail entries (Zuhause, Favoriten,
Energie, Notizen, Wartung) and 5 mobile tabs; the code explicitly rules out a 6th.
The Notizen page is the densest surface already. Import must not add a tab. It also
does not need to invent any card UI — the interactive **action-card** pipeline
(`renderActionCard` → `runAction` → `POST /api/action-callback` → `action_cards`
registry, plus `inject_message` for server-push) is already in production for
Wartung.

## Decision

**No new tab or rail entry.** Import lives on existing primitives:

- **Primary = action cards in the chat stream.** Upload via the existing attach
  affordance (extended to accept `.zip`); classify → plan → process → result render
  as cards where the assistant narrates. Import becomes a conversation, not a page.
- **Secondary = a collapsed "Importieren" section** on the existing Notizen page for
  discoverability + job status/history.
- **One inbox.** All incoming external data — uploads, chat attachments, and imports
  — converges on the **Posteingang** model (classify → assign → archive), extended
  with import-specific actions (→ Kalender / → Kontakte / → Einkaufen).

## How this avoids duplication

There is one "data arrived, triage it" surface (Posteingang) fed by every source,
rather than a bespoke UI per import type; and one action-card mechanism reused rather
than a new one built.

## Consequences

- Import work is UI-cheap: a `.zip`-capable upload, a registered `action_id` handler,
  and a Notizen section — no new navigation, no new card primitive.

# Design guidelines — "could my mother use it?"

The bar for every user-facing change in Solaris: **could a non-technical person
use it without being told how?** This is a *living checklist*, not an ADR — apply
it to every UI change and PR that touches a resident-facing surface
(`solaris-chat/src/solaris_chat/static/index.html`, the chat, cards, `.tool`s,
pages). If a change can't meet a rule, say why in the PR.

## The rules (checkable)

1. **Self-explaining.** Every view says what it is and what to do — no manual, no
   prior knowledge. An empty list explains what belongs there ("Noch keine
   Kontakte — tippe einen Namen"), never a blank box.
2. **Mobile-first, one-handed.** It works on a phone held in one hand. Tap targets
   ≥ ~40px, reachable; nothing depends on hover, right-click, or a wide screen.
   Cards fill the width; controls don't overflow or truncate.
3. **Plain language.** Say what a thing *does*, in plain German the resident sees.
   No jargon, no entity/DB terms, no English error codes surfaced raw. (Command
   names like `.task` may stay, but the menu describes them in plain words.)
4. **Obvious, safe actions.** The primary action is visible and labelled; the
   destructive one is confirmed and marked (🔒). State is unmistakable — colour is
   never the *only* signal (an "off" light is grey **and** says "aus").
5. **Immediate feedback, then truth.** Every tap responds at once (optimistic),
   then reconciles to the real state within ~1s (push/reconcile). Never a dead tap
   with no response.
6. **No dead ends.** Every link and button goes somewhere real; no `#`/`/#`
   placeholders. If something isn't available, disable it with a reason rather than
   linking to nowhere.
7. **One pattern, everywhere.** Reuse the established patterns — the `.tool`
   create-and-find card (ADR 0009), the one card SSOT (`renderHaCard` /
   `renderActionCard` / `renderListCell`, ADR 0011). Don't invent a divergent UI
   for the same kind of thing; a change to a card is a change in one place.
8. **Progressive disclosure.** Show the common thing first; hide advanced/rare
   controls behind a clear affordance. Don't wall of options.
9. **Forgiving.** Mistakes are recoverable — undo/cancel where sensible, double-Esc
   clears, edits are non-destructive (corrections outrank, never overwrite blindly).
10. **Consistent naming.** One word means one thing (e.g. "Kontakte" = people,
    "Anbieter" = document providers — not both). Match the labels the resident
    already learned.

## How to use it

- **Before shipping a resident-facing change**, walk the rules above; note any it
  can't meet and why.
- **On the box**, sanity-check on a phone viewport (the real target), not just the
  desktop.
- This file is referenced from `CLAUDE.md` so it's part of every session's rules.

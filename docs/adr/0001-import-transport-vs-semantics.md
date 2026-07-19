# ADR 0001 — Import has two layers: transport (ServiceBay) vs semantics (Solaris)

**Status:** Accepted

## Context

ServiceBay already ships a substantial import capability, **`diskImport`**: an
async, durable, owner-aware disk import with live progress. It mounts an external
disk/device, routes its contents through a routing-tree (inheritance, owner axis,
**dedup scope**), and applies them to target paths — rsyncing into the media tree,
into Immich (photos), into the vault — and flattens disc layouts on the way in
(e.g. audiobooks → Jellyfin). In short: **it gets bytes onto the box.**

Solaris's Google-Takeout import (epic #860) is about something different: turning
an export into *knowledge* — parsing `.ics`/`.vcf`/Keep/YouTube-history into
Radicale collections, vault notes, and OKF entities/facts. If we are not careful,
Solaris re-implements file transport that ServiceBay already owns.

## Decision

Two layers, cleanly split:

- **Transport = ServiceBay.** `diskImport` / file-share owns getting **bytes** onto
  the box: mount the disk, route by owner, rsync into `media`/Immich/vault, dedup at
  the **file level**.
- **Semantics = Solaris.** Solaris owns turning already-on-box bytes into
  **knowledge**: parse → Radicale (CalDAV/CardDAV) + vault notes + OKF
  entities/facts.

## How this avoids duplication

The two dedup layers are **non-overlapping**: file-level dedup is ServiceBay's job,
knowledge-level dedup (one album = one entity — see ADR 0003) is Solaris's. Neither
re-implements the other.

## Consequences

- Large Takeout archives **ride `diskImport`/file-share** into a watch-folder on the
  box; Solaris's importer picks them up and parses. We do **not** build a second
  bulk-transport path.
- The in-chat / browser upload affordance is only the **small, convenient** path for
  a single file — not the mechanism for a whole Takeout.
- When a Takeout category is really a file move (Google Photos → Immich, media →
  Jellyfin), it belongs on the ServiceBay transport side; Solaris only adds the
  semantic layer (deriving entities/facts from what landed).

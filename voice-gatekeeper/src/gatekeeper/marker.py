"""Immutable per-resident uid marker for Hermes session titles (#153).

Mirrors the chat proxy's marker: the gatekeeper embeds a `[uid:<8-char-hash>] `
prefix in every voice session's title at create time so voice sessions carry the
same ownership tag the chat panel filters on. Hashing keeps the raw username out
of the persisted title.

Voice caveat: the gatekeeper resolves the uid from speaker-ID (#84) when
enabled, otherwise falls back to `DEFAULT_UID` ('household' in the current
production config). Until speaker-ID is on, *every* voice turn is tagged with
the same uid, so voice isolation is single-user only — multi-user voice
separation requires #84. Speaker-ID is NOT implemented here.
"""

from __future__ import annotations

import hashlib


def uid_hash(uid: str) -> str:
    """8-char hex digest of the resident uid (not the raw username)."""
    return hashlib.sha256(uid.encode("utf-8")).hexdigest()[:8]


def marker_for(uid: str) -> str:
    """The sentinel prefix for `uid`, e.g. `[uid:1a2b3c4d] `."""
    return f"[uid:{uid_hash(uid)}] "

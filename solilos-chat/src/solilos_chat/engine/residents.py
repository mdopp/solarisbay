"""Resident identity for a personal turn (#352).

When the live voice path attributes a turn to an enrolled resident (the
gatekeeper's ECAPA+kNN resolves the speaker and stashes {transcript -> uid},
#350), the turn's uid is that resident's id instead of the shared household
uid. This module turns that uid into a system block that tells the model WHO
is speaking, so answers can address the person.

A turn is personal only when its uid is a real resident: a non-empty uid that
is neither the configured `default_uid` (the shared household identity, also
HA's fallback `user`) nor the ephemeral `guest`/`default` sentinels. For those
the block is empty and the prompt is byte-identical to before — the household
hot path (uid=household, the only live path while speaker-ID ships dormant)
sees no personalization and no prefix-cache churn.

Per-resident preferences/facts are NOT stored yet (no resident-profile store
exists). The seam is `_prefs(uid)`: it returns an empty list today, and a
future store reads here without touching the call sites.
"""

from __future__ import annotations

# uids that are not a person: the shared household identity and the ephemeral
# guest/anonymous sentinels. `default_uid` is also added at call time since it
# is operator-configurable (DEFAULT_UID, default "household").
_NON_RESIDENT = frozenset({"", "household", "guest", "default"})


def _prefs(uid: str) -> list[str]:
    """Per-resident preferences/facts for the prompt — the seam for a future
    resident-profile store. Empty until that store exists (#352 keeps the
    change minimal: identity now, prefs when there's a place to read them)."""
    return []


def identity_block(uid: str, default_uid: str = "household") -> str:
    """A system block naming the speaking resident, or '' for a non-resident.

    Empty (no personalization) when `uid` is blank, the `default_uid`, or a
    guest/anonymous sentinel — the household/guest hot path is unchanged.
    """
    if uid in _NON_RESIDENT or uid == default_uid:
        return ""
    lines = [f"Du sprichst gerade mit {uid}. Sprich diese Person persönlich an."]
    lines += [f"- {p}" for p in _prefs(uid)]
    return "\n".join(lines)

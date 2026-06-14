"""Immutable per-resident uid marker embedded in Hermes session titles (#153).

Hermes v0.16.0 does not persist the `user_id` we POST on session create (it
stores `user_id: null`), so the proxy cannot scope sessions by resident at the
database level. Instead each session title carries a sentinel prefix
`[uid:<8-char-hash>] ` minted from a hash of the caller's uid; the proxy filters
the session list/history to titles bearing the *caller's* marker and strips the
marker before the title reaches the browser.

The marker is browser-immutable by omission: the chat proxy router exposes only
GET/POST/DELETE on sessions (no PATCH/rename route), so a browser client has no
way to rewrite a title and strip the marker. The only title-write path is the
proxy's own `set_title`, which always re-injects the prefix. Do NOT add a PATCH
route — that would create a browser-reachable strip path.

Hashing (not the raw uid) keeps usernames out of the persisted title. 8 hex
chars + `[uid:] ` = 15 chars; with the human title capped well under Hermes'
100-char limit there is ample headroom.
"""

from __future__ import annotations

import hashlib
import re

_PREFIX_RE = re.compile(r"^\[uid:([0-9a-f]{8})\]\s")
# Maintenance sessions carry a distinct `[maint:<hash>] ` prefix (#229) so they
# never match the household `[uid:...]` filter — the ServiceBay-maintenance
# embed is isolated from the per-resident session list by construction.
_MAINT_PREFIX_RE = re.compile(r"^\[maint:([0-9a-f]{8})\]\s")
# Temporary/incognito sessions carry a distinct `[temp:<hash>] ` prefix (#246):
# like the maintenance marker it lives outside the `[uid:...]` namespace, so an
# ephemeral chat never surfaces in the durable per-resident session list — it
# exists only for the open tab and is deleted on close.
_TEMP_PREFIX_RE = re.compile(r"^\[temp:([0-9a-f]{8})\]\s")


def uid_hash(uid: str) -> str:
    """8-char hex digest of the resident uid (not the raw username)."""
    return hashlib.sha256(uid.encode("utf-8")).hexdigest()[:8]


def marker_for(uid: str) -> str:
    """The sentinel prefix for `uid`, e.g. `[uid:1a2b3c4d] `."""
    return f"[uid:{uid_hash(uid)}] "


def maint_marker(uid: str) -> str:
    """The maintenance-session prefix for `uid`, e.g. `[maint:1a2b3c4d] ` (#229).

    A distinct marker class from `marker_for`: maintenance sessions created via
    the ServiceBay-maintenance embed must never surface in the household session
    list, so they are tagged out of the `[uid:...]` namespace `has_marker`
    filters on.
    """
    return f"[maint:{uid_hash(uid)}] "


def temp_marker(uid: str) -> str:
    """The temporary/incognito-session prefix for `uid`, e.g. `[temp:1a2b3c4d] ` (#246).

    A distinct marker class from `marker_for`: ephemeral chats must never appear
    in the durable household session list, so they are tagged out of the
    `[uid:...]` namespace `has_marker` filters on — just like maintenance.
    """
    return f"[temp:{uid_hash(uid)}] "


def is_temp(title: str) -> bool:
    """True when `title` carries a `[temp:<hash>] ` marker (an ephemeral chat)."""
    return _TEMP_PREFIX_RE.match(title) is not None


def embed(uid: str, title: str) -> str:
    """Prefix `title` with the caller's marker, replacing any existing one.

    Idempotent: re-embedding an already-marked title swaps the marker rather
    than stacking prefixes, so `set_title` re-injection never double-tags.
    """
    return marker_for(uid) + strip(title)


def strip(title: str) -> str:
    """Drop a leading `[uid:xxxxxxxx] ` marker, returning the human title."""
    return _PREFIX_RE.sub("", title, count=1)


def has_marker(uid: str, title: str) -> bool:
    """True when `title` carries exactly `uid`'s marker.

    A title with no marker (legacy/pre-existing session) returns False: such
    sessions cannot be attributed to a resident, so they are hidden from every
    per-resident list rather than leaked to an arbitrary caller (privacy-safe
    default). A title marked for a *different* uid also returns False.
    """
    m = _PREFIX_RE.match(title)
    return m is not None and m.group(1) == uid_hash(uid)

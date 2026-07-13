"""Per-device native watch-sets — non-favorited widget entities feed ha_watch.

Native Android widgets can watch HA entities that a resident has NOT web-favorited
(#810). The favorites-backed `pinned_entity_owners` is empty for those, so
`ha_watch` would publish nothing. This in-memory store holds, per paired device,
the entity set the device's widgets currently want, with a TTL: the app re-POSTs
`/napi/portal/watch` while widgets exist, so an app that stops (uninstalled,
widget removed) simply lets its set expire — no favorites row, no portal entry,
nothing to clean up. Ephemeral by design, so no DB table: a chat restart drops
the sets and the next widget refresh re-POSTs them within the TTL.

`ha_watch` unions `native_watch_owners()` into its pinned owner map at its
re-derive interval; a watched entity's owner is the device's uid, so a state
change publishes `card_state` to that uid over the existing SSE.
"""

from __future__ import annotations

import time

# The app re-POSTs while widgets exist; a device that goes quiet expires within
# this window. ~60 min balances "survives a network blip" against "a removed
# widget stops flowing soon".
TTL_S = 60.0 * 60.0


class NativeWatchStore:
    def __init__(self, ttl_s: float = TTL_S) -> None:
        self._ttl_s = ttl_s
        # device_id -> (uid, frozenset[entity_id], expires_at)
        self._sets: dict[str, tuple[str, frozenset[str], float]] = {}

    def set(self, device_id: str, uid: str, entity_ids: set[str]) -> None:
        """Store/REPLACE this device's watch-set with a fresh TTL."""
        self._sets[device_id] = (
            uid,
            frozenset(entity_ids),
            time.monotonic() + self._ttl_s,
        )

    def native_watch_owners(self) -> dict[str, set[str]]:
        """Every watched entity → the uids of the (non-expired) devices watching it.

        Same shape as `favorites_store.pinned_entity_owners`, so `ha_watch` can
        union the two. Expired sets are dropped here so the map stays current
        without a separate sweep task."""
        now = time.monotonic()
        expired = [dev for dev, (_, _, exp) in self._sets.items() if exp <= now]
        for dev in expired:
            del self._sets[dev]
        out: dict[str, set[str]] = {}
        for uid, entities, _ in self._sets.values():
            for entity_id in entities:
                out.setdefault(entity_id, set()).add(uid)
        return out

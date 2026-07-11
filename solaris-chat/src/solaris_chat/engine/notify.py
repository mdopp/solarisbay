"""Web Push notifier — fan a timer/reminder out to a resident's phones (#713).

The timer scheduler's speaker announce stays primary; this is a best-effort
second channel so a reminder reaches the resident away from the Voice PE
speaker, through the installed PWA's service worker (standard Web Push / VAPID,
no Google/FCM). Constructed once at boot and injected into `TimerScheduler`.

No-op when VAPID is unset (`enabled` is False): the keys are an operator
prerequisite, not in the repo, so nothing breaks on the box before they are
configured — `push()` returns immediately. Errors are logged and swallowed; a
404/410 prunes the dead endpoint. The notifier must never break the timer loop.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from solaris_chat import push_store
from solaris_chat.logging import log


class Notifier:
    def __init__(
        self,
        db_path: str,
        vapid_public_key: str = "",
        vapid_private_key: str = "",
        vapid_subject: str = "",
    ):
        self._db_path = db_path
        self._public_key = vapid_public_key
        self._private_key = vapid_private_key
        self._subject = vapid_subject or "mailto:admin@solaris.local"

    @property
    def enabled(self) -> bool:
        return bool(self._public_key and self._private_key)

    async def push(
        self, uid: str, title: str, body: str, data: dict[str, Any] | None = None
    ) -> None:
        """Send a notification to every device the resident registered.

        No-op without VAPID. Prunes an endpoint the browser reports gone
        (404/410); every other failure is logged and swallowed."""
        if not self.enabled:
            return
        subs = push_store.list_for_uid(self._db_path, uid)
        if not subs:
            return
        payload = json.dumps({"title": title, "body": body, "data": data or {}})
        for sub in subs:
            await asyncio.to_thread(self._send_one, sub, payload)

    def _send_one(self, sub: dict[str, Any], payload: str) -> None:
        from pywebpush import WebPushException, webpush

        endpoint = sub["endpoint"]
        try:
            webpush(
                subscription_info={
                    "endpoint": endpoint,
                    "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]},
                },
                data=payload,
                vapid_private_key=self._private_key,
                vapid_claims={"sub": self._subject},
            )
            push_store.mark_ok(self._db_path, endpoint)
        except WebPushException as e:
            status = getattr(getattr(e, "response", None), "status_code", None)
            if status in (404, 410):
                push_store.remove_by_endpoint(self._db_path, endpoint)
                log.info("engine.push.pruned", endpoint=endpoint, status=status)
            else:
                log.error("engine.push.failed", endpoint=endpoint, error=str(e))
        except Exception as e:  # noqa: BLE001 — push must never break the timer loop
            log.error("engine.push.error", endpoint=endpoint, error=str(e))

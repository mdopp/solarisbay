"""Minimal structured logger — one JSON object per line on stdout.

Matches the gatekeeper's logging shape so the two Solilos Python containers
read the same way in `podman logs` / ServiceBay's log view.
"""

from __future__ import annotations

import json
import sys
import time


class _Log:
    def _emit(self, level: str, event: str, **fields: object) -> None:
        record = {"ts": round(time.time(), 3), "level": level, "event": event}
        record.update(fields)
        sys.stdout.write(json.dumps(record, default=str) + "\n")
        sys.stdout.flush()

    def info(self, event: str, **fields: object) -> None:
        self._emit("info", event, **fields)

    def warn(self, event: str, **fields: object) -> None:
        self._emit("warn", event, **fields)

    def error(self, event: str, **fields: object) -> None:
        self._emit("error", event, **fields)


log = _Log()

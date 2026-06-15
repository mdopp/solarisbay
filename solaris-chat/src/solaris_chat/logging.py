"""Structured-logging helper for the chat proxy.

Emits the same JSON-line shape the gatekeeper does (ServiceBay's logger
contract): one object per line on stdout. Kept as its own tiny module so
the proxy carries no logging dependency.
"""

from __future__ import annotations

import datetime
import json
import os
import sys
from typing import Any


class _Logger:
    def __init__(self, tag: str) -> None:
        self._tag = tag

    def _emit(self, level: str, message: str, **args: Any) -> None:
        record = {
            "ts": datetime.datetime.now().astimezone().isoformat(),
            "level": level,
            "tag": self._tag,
            "message": message,
            "args": args,
        }
        sys.stdout.write(json.dumps(record, default=str) + "\n")
        sys.stdout.flush()

    def debug(self, message: str, **args: Any) -> None:
        self._emit("debug", message, **args)

    def info(self, message: str, **args: Any) -> None:
        self._emit("info", message, **args)

    def warn(self, message: str, **args: Any) -> None:
        self._emit("warn", message, **args)

    def error(self, message: str, **args: Any) -> None:
        self._emit("error", message, **args)


log = _Logger(os.environ.get("SOLARIS_COMPONENT", "chat"))

#!/usr/bin/env python3
"""
Migration: llama v2 → v3.

Router mode (#1416): one llama-server serves four presets and the client picks
with the `model` field of its request. The pod's argv loses every model option
to `presets.ini`, which post-deploy writes beside the weights.

The port moves with it. The router binds `127.0.0.1:${LLAMA_ROUTER_PORT}`
(11434) and post-deploy's mode policy proxy takes `${LLAMA_PORT}` (11435) —
same wide bind, same `blockLanAccess` rule, plus the lease's `allowed` set. No
consumer changes: 11435 is still the address in `LLAMA_SERVER_URL` and in PI
WEB's `models.json`. The pod is recreated on this deploy, so there is a few
seconds between the old bind going and the proxy coming up.

The one thing on disk that carries the old shape is
`${DATA_DIR}/solarisbay/llama-profile.json` — the household profile a release
reloads. Its `cache_type` became `cache_type_k`/`cache_type_v` (the 27B runs
q8 keys with q4 values since #1415) and `reasoning` is gone (thinking is a
per-request switch now). An unmigrated record would simply lose those keys on
the next read, so this is tidiness rather than rescue — but a half-known
schema on disk is exactly what the next reader trips over.
"""

from __future__ import annotations

import json
import os
import sys

DEFAULT_DATA_DIR = "/mnt/data/stacks"


def migrate(path: str) -> str:
    if not os.path.exists(path):
        return "no household profile on disk; nothing to migrate"
    try:
        with open(path, encoding="utf-8") as f:
            record = json.load(f)
    except (OSError, ValueError) as e:
        return f"could not read {path} ({e}); post-deploy rewrites it on this deploy"
    if not isinstance(record, dict):
        return f"{path} is not an object; post-deploy rewrites it on this deploy"
    cache_type = record.pop("cache_type", "")
    record.pop("reasoning", None)
    record.setdefault("cache_type_k", cache_type)
    record.setdefault("cache_type_v", cache_type)
    record.setdefault("ubatch", "")
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(record, f)
    except OSError as e:
        return f"could not write {path} ({e}); post-deploy rewrites it on this deploy"
    return f"{path}: cache_type split into cache_type_k/cache_type_v, reasoning dropped"


def main() -> int:
    data_dir = os.environ.get("DATA_DIR") or DEFAULT_DATA_DIR
    print("Llama v2 → v3: llama-server now runs in router mode.")
    print("  - One process, one port, four presets (gemma-4-e4b, gemma-4-12b,")
    print("    qwen3.6-35b-a3b, qwen3.8-27b); the client picks per request.")
    print("  - A lease no longer swaps the server: it sets the environment and")
    print("    the presets the mode allows (written as `allowed` in the lease).")
    print("  - The router moved to 127.0.0.1:11434; the mode policy proxy")
    print("    (solaris-llama-policy.service) holds 11435 and refuses a preset")
    print("    the standing mode does not allow. No client address changes.")
    print("  - `--reasoning off` is gone; thinking is a per-request switch.")
    print("  " + migrate(os.path.join(data_dir, "solarisbay", "llama-profile.json")))
    return 0


if __name__ == "__main__":
    sys.exit(main())

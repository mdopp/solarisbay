"""A clearly-labeled VRAM-headroom *estimate* for the model picker (#367).

The panel lets an admin pull arbitrary models, so it needs a rough sense of
whether the currently-selected models fit the hardware before one blows the
GPU. This is a heuristic, not an accounting: a model's loaded VRAM footprint
exceeds its on-disk size (KV cache, context, runner overhead), so we apply a
flat headroom factor to the disk size when a model isn't already loaded.

Total/used VRAM is sourced, in order:
  1. ServiceBay's node resources (`get_system_info` → `resources.gpus[0]`,
     queried over the admin MCP) — the real total *and* used reported by the
     node agent's `nvidia-smi`, so it includes KV-cache/runner overhead the
     chat container can't see (it has no GPU/nvidia-smi of its own).
  2. `GPU_TOTAL_VRAM` env (operator override, bytes) minus what `/api/ps`
     reports as already resident.
  3. `nvidia-smi` queried total/used (only if it somehow runs in-container).
  4. unknown -> we still report the combined need so the admin sees the size,
     just without a fit verdict.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import Any

# A loaded model needs more VRAM than its file size (KV cache + context +
# runner). 1.2x is a deliberately rough cushion — this is an estimate.
_OVERHEAD = 1.2


def combined_selected_bytes(
    selected: list[str],
    tags: list[dict[str, Any]],
    ps: list[dict[str, Any]],
) -> int:
    """Estimated combined VRAM for the distinct selected model tags.

    A model already in `/api/ps` contributes its measured `size_vram`; one only
    on disk (`/api/tags`) contributes `size * overhead`; an unknown tag (not
    pulled yet) contributes 0 — it has no size to estimate from.
    """
    ps_vram = {
        m["name"]: m["size_vram"]
        for m in ps
        if isinstance(m, dict) and isinstance(m.get("size_vram"), int)
    }
    disk = {
        m["name"]: m["size"]
        for m in tags
        if isinstance(m, dict) and isinstance(m.get("size"), int)
    }
    total = 0
    for tag in dict.fromkeys(selected):  # distinct, order-stable
        if tag in ps_vram:
            total += ps_vram[tag]
        elif tag in disk:
            total += int(disk[tag] * _OVERHEAD)
    return total


async def servicebay_gpu(url: str, token_path: str) -> tuple[int, int] | None:
    """`(total, used)` GPU VRAM in bytes from ServiceBay's node resources.

    The node agent already runs `nvidia-smi` and surfaces it under
    `resources.gpus[]` (name/memoryTotal/memoryUsed/…). We sum across GPUs.
    Fail-open: any error (MCP unreachable, no token, no GPU) returns None and
    the caller falls back to the env/nvidia-smi sources.
    """
    if not url:
        return None
    try:
        from solaris_chat.engine.tools.mcp_tools import call_sb_tool

        raw = await call_sb_tool(url, token_path, "get_system_info", {})
        data = json.loads(raw)
    except Exception:  # noqa: BLE001 — fail-open: no SB GPU => other sources
        return None
    res = data.get("resources", data) if isinstance(data, dict) else {}
    gpus = res.get("gpus") if isinstance(res, dict) else None
    if not isinstance(gpus, list) or not gpus:
        return None
    total = used = 0
    for g in gpus:
        if not isinstance(g, dict):
            continue
        t, u = g.get("memoryTotal"), g.get("memoryUsed")
        if isinstance(t, int) and t > 0:
            total += t
        if isinstance(u, int) and u >= 0:
            used += u
    return (total, used) if total else None


def _nvidia_smi_total_used() -> tuple[int, int] | None:
    """`(total, used)` GPU VRAM in bytes from `nvidia-smi`, or None."""
    if not shutil.which("nvidia-smi"):
        return None
    try:
        out = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=memory.total,memory.used",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0 or not out.stdout.strip():
        return None
    # Sum across GPUs; values are MiB.
    total = used = 0
    for line in out.stdout.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) != 2 or not all(p.isdigit() for p in parts):
            return None
        total += int(parts[0]) * 1024 * 1024
        used += int(parts[1]) * 1024 * 1024
    return (total, used) if total else None


def available_bytes(ps: list[dict[str, Any]]) -> int | None:
    """Estimated free VRAM in bytes, or None when no source is available.

    `GPU_TOTAL_VRAM` (env, bytes) wins: free = total - what `/api/ps` says is
    already resident. Otherwise fall back to a queried `nvidia-smi` total/used.
    """
    env_total = os.environ.get("GPU_TOTAL_VRAM")
    if env_total and env_total.strip().isdigit() and int(env_total) > 0:
        resident = sum(
            m["size_vram"]
            for m in ps
            if isinstance(m, dict) and isinstance(m.get("size_vram"), int)
        )
        return max(int(env_total) - resident, 0)
    smi = _nvidia_smi_total_used()
    if smi is not None:
        total, used = smi
        return max(total - used, 0)
    return None

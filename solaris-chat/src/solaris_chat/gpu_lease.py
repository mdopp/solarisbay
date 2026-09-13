"""The whole-card GPU lease (#1320).

The coding run (Qwen 3.8 27B, 15.0 GB) needs the entire 16.4 GB card — it does
not fit beside Solaris' own llama-server (3.9 GB), let alone the voice stack.
The operator's decision of 2026-09-05 is that such a job takes it on request,
with no time window and no presence check, and that Solaris says so instead of
hanging.

`gpu-lease.py acquire <holder>` on the box writes this file and then stops the
voice stack, the embeddings server and `llama.service`; `release` starts them
again, waits for llama-server's `/health` (the household model is warm) and only
then removes it. Its presence is therefore exactly "the household model is not
loaded", and reading it costs a stat instead of a request to a server that is
not running.

Since #1319 a lease also has a **mode** and a **deadline**:

* `exclusive` — the shape above: nothing answers, so a turn gets one honest
  German sentence instead of a timeout against a dead socket.
* `coding` — the card goes to the coding model, but llama-server keeps serving
  it, so Solaris answers the household from that model for the window and the
  chat carries a banner naming it. Only the swap itself mutes (`ready: false`).
* `foundry` (#1325) — llama-server runs Gemma 4 12B instead of the household
  e4b for a foundry evening. The voice stack keeps the GPU and nothing about
  the house changes except that answers take about a second longer, so the
  operator ruled there is no banner either: this one is named in `/api/whoami`
  for the log and shows the resident nothing.
* `thinking` (#1416) — the card goes to Qwen 3.6 35B-A3B for reading and
  logic. Same shape as `coding`: the voice stack moves to the CPU and Solaris
  keeps answering, from the MoE.

Since #1416 llama-server runs as a **router**: one process holds all four
presets and the client picks with the `model` field of its request, so a named
mode no longer swaps the server. What the mode now decides is which presets a
client may ask for — the box writes that set into the lease as `allowed` — and
which of them Solaris itself answers from. That is why the preset name is read
out of the lease here rather than taken from whatever the caller calls the
model: `FAST_MODEL` is still the Ollama-era tag the panel and the traces use,
and the router has never heard of it.

The deadline is enforced on the box by a transient systemd timer that runs
`release`, not here — an end signal alone was not enough in #1260. What this
module does with `until` is show the resident when their assistant is back to
normal.
"""

from __future__ import annotations

import json
from pathlib import Path

LEASE_FILENAME = "gpu_lease.json"

# The lease modes in which llama-server is still serving something, so the turn
# goes to the model instead of to the fixed sentence.
ANSWERING_MODES = ("coding", "foundry", "thinking")

# What the router is asked for when no lease stands, and the preset each mode
# answers from when the lease file names none. The box writes the same strings
# in `templates/llama/post-deploy.py`; a standing lease's own `alias` wins over
# this table, so an operator who deployed other weights is asked for those.
HOUSEHOLD_PRESET = "gemma-4-e4b"
MODE_PRESETS = {
    "foundry": "gemma-4-12b",
    "thinking": "qwen3.6-35b-a3b",
    "coding": "qwen3.8-27b",
}

# What the resident hears while another job holds the card. A fixed sentence,
# because the model that would phrase something friendlier is the one that is
# unloaded: it says what is happening and when to come back, and nothing else.
BUSY_REPLY = (
    "Ich rechne gerade an einer großen Aufgabe und brauche dafür die ganze "
    "Grafikkarte. Sobald sie frei ist, bin ich wieder da — frag mich in ein "
    "paar Minuten noch einmal."
)


def lease_path(db_path: str) -> Path:
    """The lease file beside `solaris.db` — the chat pod mounts that directory,
    and the box writes into the same host path."""
    return Path(db_path).parent / LEASE_FILENAME


def is_leased(path: str | Path) -> bool:
    """True while another job holds the card.

    The file existing *is* the lease: a truncated or half-written one still
    means the units are stopped, so it counts as held rather than being read
    as no lease and answered into a dead socket.
    """
    return bool(path) and Path(path).exists()


def record(path: str | Path) -> dict:
    """The lease exactly as the box wrote it, `{}` for anything unreadable."""
    try:
        data = json.loads(Path(path).read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def holder(path: str | Path) -> str:
    """Who holds it, for the log line; `""` when the file says nothing."""
    name = record(path).get("holder")
    return name.strip() if isinstance(name, str) else ""


def mutes_chat(path: str | Path) -> bool:
    """True when no model can answer this turn.

    A `coding` (#1319) or `foundry` (#1325) lease does not mute: llama-server
    is serving that model and the household turn goes to it. The exception is
    the swap itself — `ready` is false while the leased model is still
    loading, and those ~2 minutes are exactly the dead socket the fixed
    sentence exists for. Anything unreadable counts as muting, as in #1320.
    """
    if not is_leased(path):
        return False
    lease = record(path)
    return lease.get("mode") not in ANSWERING_MODES or not lease.get("ready")


def state(path: str | Path) -> dict | None:
    """What the chat surface shows about the lease, or `None` when there is
    none. `until` is epoch seconds — the browser formats it in local time."""
    if not is_leased(path):
        return None
    lease = record(path)
    until = lease.get("until")
    held = lease.get("mode")
    return {
        "mode": held if held in ANSWERING_MODES else "exclusive",
        "model": str(lease.get("model") or ""),
        # The `--alias` llama-server answers with while this lease stands
        # (#1333) — the same string `/api/model-lease` and the `model` field of
        # a `/v1` response carry, so the three surfaces cannot disagree.
        "alias": str(lease.get("alias") or ""),
        "until": float(until) if isinstance(until, (int, float)) else 0.0,
        "answers": not mutes_chat(path),
    }


def mode(path: str | Path) -> str:
    """The standing lease mode, `""` when the household has the card."""
    if not is_leased(path):
        return ""
    name = record(path).get("mode")
    return name if name in ANSWERING_MODES else ""


def preset(path: str | Path) -> str:
    """The router preset this turn goes to (#1416).

    The `model` field of a `/v1` request is load-bearing now that one server
    serves four models, so it is this — not the caller's own name for the
    model — that goes on the wire.
    """
    held = mode(path)
    if not held:
        return HOUSEHOLD_PRESET
    alias = record(path).get("alias")
    if isinstance(alias, str) and alias.strip():
        return alias.strip()
    return MODE_PRESETS[held]


def allowed(path: str | Path) -> list[str]:
    """The presets a client may ask the router for while this lease stands.

    Written by the box as `allowed`; the mode's own preset is the fallback for
    a lease file from before #1416, so an upgrade in flight never reads as
    "nothing is allowed".
    """
    held = mode(path)
    if not held:
        return [HOUSEHOLD_PRESET]
    names = [
        name.strip()
        for name in record(path).get("allowed") or []
        if isinstance(name, str) and name.strip()
    ]
    return names or [MODE_PRESETS[held]]


def thinks(path: str | Path) -> bool:
    """True while the `thinking` mode stands — the one mode whose whole point
    is the reasoning trace, so the Engine asks for it per request (#1416)."""
    return mode(path) == "thinking"

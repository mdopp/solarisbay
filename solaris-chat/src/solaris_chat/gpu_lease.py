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

Since #1319 a lease also has a **mode** and a **deadline**, and since #1435
there are three of them:

* `exclusive` — the shape above: nothing answers, so a turn gets one honest
  German sentence instead of a timeout against a dead socket.
* `foundry` (#1325) — llama-server answers the house from Gemma 4 12B instead
  of the household e4b. The voice stack keeps the GPU and nothing about the
  house changes except that answers take about a second longer, so the operator
  ruled there is no banner either: this one is named in `/api/whoami` for the
  log and shows the resident nothing.
* `erweitert` — the card is free for a bigger model. llama-server keeps
  serving, so Solaris answers the household from whatever preset is loaded and
  the chat carries a banner naming it. Only the swap itself mutes
  (`ready: false`). The voice stack runs on the CPU for the window and the
  embeddings server is down, so the vault's semantic search pauses.
* no lease at all — `haushalt`: the card is the house's, e4b answers.

`thinking` and `coding` (#1416/#1319) were two more names for what `erweitert`
does; they are read as `erweitert`, so a lease file written before the upgrade
is understood rather than mistaken for no lease at all. `foundry` is not one of
them — it sets a different environment and stayed a mode of its own.

Since #1416 llama-server runs as a **router**: one process holds all presets
and the client picks with the `model` field of its request, so a mode no longer
swaps the server. What the mode decides is which presets a client may ask for —
the box writes that set into the lease as `allowed` — and which of them Solaris
itself answers from. That is why the preset name is read out of the lease here
rather than taken from whatever the caller calls the model: `FAST_MODEL` is
still the Ollama-era tag the panel and the traces use, and the router has never
heard of it.

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
# goes to the model instead of to the fixed sentence. `haushalt` is the absence
# of a lease and is never written to a lease file.
FOUNDRY_MODE = "foundry"
EXTENDED_MODE = "erweitert"
ANSWERING_MODES = (FOUNDRY_MODE, EXTENDED_MODE)

# The names `erweitert` had before #1435. A lease file on the box outlives the
# deploy that collapsed them, so they are read rather than refused. `foundry`
# is deliberately absent: it keeps the voice stack on the GPU and is its own
# mode.
MODE_ALIASES = {
    "thinking": EXTENDED_MODE,
    "coding": EXTENDED_MODE,
}

# What the router is asked for when no lease stands, and the preset each named
# mode or retired name has always meant — still promised to a caller that sends
# one, which is how foundry-chronicle#321 keeps working. The box writes the same
# strings in `templates/llama/post-deploy.py`; a standing lease's own `alias`
# wins over this table, so an operator who deployed other weights, or a client
# that picked its own preset, is asked for that one.
HOUSEHOLD_PRESET = "gemma-4-e4b"
MODE_PRESETS = {
    "foundry": "gemma-4-12b",
    "thinking": "qwen3.6-35b-a3b",
    "coding": "qwen3.8-27b",
}

# The name a resident reads for a preset. The tile and the chat banner say
# these; anything not listed shows as the preset id itself, which is still
# better than "ein anderes Modell".
PRESET_LABELS = {
    "gemma-4-e4b": "Gemma 4 e4b",
    "gemma-4-12b": "Gemma 4 12B",
    "qwen3.6-35b-a3b": "Qwen 35B",
    "qwen3.8-27b": "Qwen 27B",
}


def canonical_mode(name: object) -> str:
    """The mode a lease file names, under the name it has today (#1435)."""
    text = name.strip() if isinstance(name, str) else ""
    return MODE_ALIASES.get(text, text)


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

    An `erweitert` window does not mute: llama-server is serving a model and
    the household turn goes to it. The exception is the swap itself — `ready`
    is false while the window is still being set up, and those ~2 minutes are
    exactly the dead socket the fixed sentence exists for. Anything unreadable
    counts as muting, as in #1320.
    """
    if not is_leased(path):
        return False
    lease = record(path)
    return canonical_mode(lease.get("mode")) not in ANSWERING_MODES or not lease.get(
        "ready"
    )


def state(path: str | Path) -> dict | None:
    """What the chat surface shows about the lease, or `None` when there is
    none. `until` is epoch seconds — the browser formats it in local time."""
    if not is_leased(path):
        return None
    lease = record(path)
    until = lease.get("until")
    held = canonical_mode(lease.get("mode"))
    alias = str(lease.get("alias") or "")
    return {
        "mode": held if held in ANSWERING_MODES else "exclusive",
        # The model that is actually loaded, said the way a resident reads it:
        # the preset the door last served, else what the window was taken for.
        "model": PRESET_LABELS.get(alias) or str(lease.get("model") or ""),
        # Who took the window (#1435): `erweitert` is the state in which the
        # house is not served first, so the banner has to be able to say that
        # somebody else has the card and who — otherwise a voice assistant that
        # has gone slow looks broken.
        "holder": str(lease.get("holder") or ""),
        # The `--alias` llama-server answers with while this lease stands
        # (#1333) — the same string `/api/model-lease` and the `model` field of
        # a `/v1` response carry, so the three surfaces cannot disagree.
        "alias": alias,
        "until": float(until) if isinstance(until, (int, float)) else 0.0,
        "answers": not mutes_chat(path),
    }


def mode(path: str | Path) -> str:
    """The standing lease mode, `""` when the household has the card."""
    if not is_leased(path):
        return ""
    name = canonical_mode(record(path).get("mode"))
    return name if name in ANSWERING_MODES else ""


def preset(path: str | Path) -> str:
    """The router preset this turn goes to (#1416).

    The `model` field of a `/v1` request is load-bearing now that one server
    serves several models, so it is this — not the caller's own name for the
    model — that goes on the wire.

    In `erweitert` the holder picks the model, and the policy proxy records
    which preset it is serving in the lease's `alias` (#1435). Following that
    is what keeps Solaris from asking for e4b every turn and evicting the very
    model the holder is working with. Until anything has been asked for, the
    preset the mode name means — else the household one — is the sane default.
    """
    if not mode(path):
        return HOUSEHOLD_PRESET
    alias = record(path).get("alias")
    if isinstance(alias, str) and alias.strip():
        return alias.strip()
    legacy = MODE_PRESETS.get(str(record(path).get("mode") or ""))
    return legacy or HOUSEHOLD_PRESET


def allowed(path: str | Path) -> list[str]:
    """The presets a client may ask the router for while this lease stands.

    Written by the box as `allowed`; the preset the mode name means is the
    fallback for a lease file from before #1416, so an upgrade in flight never
    reads as "nothing is allowed".
    """
    if not mode(path):
        return [HOUSEHOLD_PRESET]
    names = [
        name.strip()
        for name in record(path).get("allowed") or []
        if isinstance(name, str) and name.strip()
    ]
    legacy = MODE_PRESETS.get(str(record(path).get("mode") or ""))
    return names or [legacy or HOUSEHOLD_PRESET]


def thinks(path: str | Path) -> bool:
    """True while `erweitert` stands — the only window in which a model that
    can reason may be loaded, so a resident who asks for deliberation in words
    gets it. `foundry` allows the 12B and the e4b and neither reasons, so it is
    not one. `enable_thinking` stays a per-request switch (#1416/#1435).
    """
    return mode(path) == EXTENDED_MODE

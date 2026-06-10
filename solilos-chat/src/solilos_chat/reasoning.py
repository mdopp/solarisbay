"""Per-turn reasoning-effort selection for chat turns (#222 / #224).

Measured on the box: a household tool turn ("welche Lichter sind an") spent
~90% of its 12-15s budget generating a reasoning block. `reasoning_effort:
"none"` over Hermes' /v1 cuts the SAME tool call to ~1.35s (`extra_body.think`
had no effect — `reasoning_effort` is the working knob). The live Hermes config
has `show_reasoning: false`, so reasoning was generated but never surfaced;
turning it on per-turn is what lets the chat render the thinking block.

Two inputs decide the effort, in priority order:

1. The per-conversation **selector** the operator picks in the UI (#224) —
   "Schnell" / "Gründlich" mapping to "none" / "high" (and "low" if offered).
   This ALWAYS wins for that conversation; the operator's explicit choice is
   never second-guessed.
2. The **adaptive default** (#222) when no selector value is sent. Default FAST
   ("none") for everything — household turns are mostly tool-control. We
   escalate to thorough only on a strong, explicit signal: an admin/diagnose
   context, or the resident asking for it in words. Deliberately conservative —
   NOT a complexity classifier (a fragile one would misroute the common control
   turns into the slow path, the exact pain we are fixing).

When the chosen effort is reasoning (not "none") we also ask Hermes to surface
the reasoning block (`show_reasoning: true`) so the chat UI can render it; a
fast turn sends neither and stays clean.
"""

from __future__ import annotations

import re

# reasoning_effort values understood by the provider (OpenAI-compatible).
FAST = "none"
LOW = "low"
HIGH = "high"


# Values a UI selector may legitimately send. Anything else is ignored and the
# adaptive default applies (defensive — the body is client-controlled).
_SELECTOR_VALUES = {FAST, LOW, HIGH}

# Explicit "please think harder" cues, German + English (mirrors the voice
# path's list). Word-boundaried, case-insensitive, intentionally narrow — tune
# by adding a phrase, not by widening into heuristics.
_THINK_CUES = re.compile(
    r"\b("
    r"denk(e)? (mal )?(scharf |gut |genau )?nach|"
    r"denk(e)? (gr[üu]ndlich|sorgf[äa]ltig)|"
    r"erkl[äa]r(e)? (mir |das |es |mir das )*genau|"
    r"begr[üu]nde( das)?|"
    r"think (it |this )?(through|hard|carefully|step by step)|"
    r"reason (it |this )?through|"
    r"explain (it |this )?(in detail|thoroughly)"
    r")\b",
    re.IGNORECASE,
)


def wants_reasoning(text: str) -> bool:
    """True when the message carries an explicit 'think harder' cue."""
    return bool(_THINK_CUES.search(text or ""))


def normalize_selector(value: object) -> str | None:
    """Return the selector value if it's a recognised reasoning_effort, else
    None (apply the adaptive default). Tolerates any client payload."""
    return value if value in _SELECTOR_VALUES else None


def choose_effort(text: str, *, selector: object = None, admin: bool = False) -> str:
    """Pick the reasoning_effort for a chat turn.

    The per-conversation `selector` (UI, #224) overrides everything. Otherwise
    the adaptive default (#222): FAST, escalated to HIGH only for an admin
    context (diagnose/soul work) or an explicit reasoning cue in the message.
    """
    chosen = normalize_selector(selector)
    if chosen is not None:
        return chosen
    if admin or wants_reasoning(text):
        return HIGH
    return FAST

"""Per-turn reasoning-effort selection for voice turns (#222).

Measured on the box: a household tool turn ("welche Lichter sind an") spent
~90% of its 12-15s budget generating a reasoning block that the operator never
sees. `reasoning_effort: "none"` over the engine's /v1 cuts the SAME tool call to
~1.35s (`extra_body.think=False` had no effect — `reasoning_effort` is the
working knob). Voice is household *control*; almost every turn is a tool action
that wants the fast path.

So the default is `"none"` (fast) for everything. We escalate to real reasoning
only on a strong, explicit signal: the resident asks for it in words ("denk
nach", "erkläre genau", "think it through", …). Keep this list short and
obvious — this is a deliberately conservative cue match, NOT a complexity
classifier (a fragile classifier would misroute the common control turns into
the slow path, which is exactly the pain we are fixing).
"""

from __future__ import annotations

import re

# Reasoning-effort values understood by the provider (OpenAI-compatible). The
# selector only ever returns these; `None` means "send no override" (used when
# the value is already the proxy default and we want to keep the body minimal).
FAST = "none"
THOROUGH = "high"

# Explicit "please think harder" cues, German + English. A match on any of
# these in the transcript escalates that turn to THOROUGH. Word-boundaried and
# case-insensitive; intentionally narrow so a normal control phrase never trips
# it. Tune by adding a phrase here, not by widening into heuristics.
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
    """True when the transcript carries an explicit 'think harder' cue."""
    return bool(_THINK_CUES.search(text or ""))


def choose_effort(text: str) -> str:
    """Pick the reasoning_effort for a voice turn.

    Default FAST (`"none"`); THOROUGH only on an explicit reasoning cue. There
    is no manual selector on the voice path (no UI), so the cue is the only
    escalation — which keeps the common control turn on the fast path.
    """
    return THOROUGH if wants_reasoning(text) else FAST

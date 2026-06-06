"""Shipped personality catalog.

Hermes 0.15.1 has no personalities API and the session chat endpoint does
not interpret `/personality` slash commands (they arrive as plain user
text). What it *does* accept is a `system_prompt` on session **create**
(PATCH rejects it), so a personality here is a named overlay prompt the
proxy attaches when a session is born. The overlay rides on top of the one
global SOUL.md — it shapes tone/structure, it does not replace the soul.

Selection is per-session and open to every resident (no admin gate): the
browser picks an id on a new session and the proxy maps it to the prompt.
`sol` is the default and carries no overlay (pure SOUL.md).
"""

from __future__ import annotations

from typing import NamedTuple


class Personality(NamedTuple):
    id: str
    label: str
    description: str
    system_prompt: str  # empty => no overlay (pure SOUL.md)


PERSONALITIES: list[Personality] = [
    Personality(
        id="sol",
        label="Sol",
        description="The default voice — warm, clear, the soul as written.",
        system_prompt="",
    ),
    Personality(
        id="concise",
        label="Concise",
        description="Fewest words that fully answer. Bullets over prose, no preamble.",
        system_prompt=(
            "Answer in the fewest words that fully address the request. "
            "Prefer short sentences and bullet points. Skip preamble and "
            "sign-off. Never pad."
        ),
    ),
    Personality(
        id="technical",
        label="Technical",
        description="Expert register — precise terms, code, exact names, no basics.",
        system_prompt=(
            "Assume an expert audience. Be precise and use correct "
            "terminology; include code, commands, and exact identifiers when "
            "relevant. Do not explain fundamentals unless asked."
        ),
    ),
    Personality(
        id="teacher",
        label="Teacher",
        description="Step-by-step for a curious beginner, with a small example.",
        system_prompt=(
            "Explain step by step for a curious beginner. Define terms in "
            "plain language, give one small concrete example, and end by "
            "checking understanding with a short question."
        ),
    ),
]

_BY_ID = {p.id: p for p in PERSONALITIES}
DEFAULT_ID = "sol"


def catalog() -> list[dict[str, str]]:
    """The browser-facing list (no system_prompt — that stays server-side)."""
    return [
        {"id": p.id, "label": p.label, "description": p.description}
        for p in PERSONALITIES
    ]


def system_prompt_for(personality_id: str | None) -> str:
    """Map a personality id to its overlay prompt; '' for default/unknown."""
    if not personality_id:
        return ""
    p = _BY_ID.get(personality_id)
    return p.system_prompt if p else ""

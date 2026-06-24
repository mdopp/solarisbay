"""Deterministic confirmation gate for sensitive HA actions (#570).

`offer_choices` + the tool-discipline prompt (u64/#558) ask the model to
confirm before opening/unlocking the house, but gemma4:e4b obeys it only
sometimes — "Garagentor öffnen" sometimes executes with a bare "Klar.". For a
safety feature "usually" is not enough, so the engine ENFORCES it in code: a
`ha_call_service` on a sensitive target is intercepted at dispatch, not run, and
held until the user's next reply confirms it.

This module owns the policy (what is sensitive, what counts as yes/no) and the
per-session stash of the pending action; the loop in client.py calls `gate()`
before dispatching a tool and consumes a stashed action at the top of a turn.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# A ha_call_service is SENSITIVE when its target can open or unsecure the house.
# Two axes so the set is explicit and easy to extend: the whole domain (any lock
# action is sensitive — locking re-secures, but unlock is the danger and gating
# both keeps the rule trivial), or a specific opening/disarming service.
SENSITIVE_DOMAINS: frozenset[str] = frozenset({"lock", "alarm_control_panel"})
SENSITIVE_SERVICES: frozenset[str] = frozenset(
    {
        "open_cover",  # cover: garage door / gate
        "open",
        "unlock",
        "alarm_disarm",
    }
)

# Affirmative / negative reply detection — a small, case-insensitive keyword set
# matched on whole words. Deliberately simple and robust: the chips offer
# ja/nein, and these cover the common spoken variants without a parser.
_AFFIRMATIVE: frozenset[str] = frozenset(
    {
        "ja",
        "jawohl",
        "jep",
        "jo",
        "klar",
        "gerne",
        "ok",
        "okay",
        "mach",
        "machs",
        "los",
        "öffne",
        "öffnen",
        "auf",
        "bestätige",
        "bestätigen",
        "yes",
        "yep",
        "yeah",
        "sure",
        "go",
    }
)
_NEGATIVE: frozenset[str] = frozenset(
    {
        "nein",
        "ne",
        "nee",
        "nö",
        "stop",
        "stopp",
        "halt",
        "abbrechen",
        "abbruch",
        "lass",
        "doch",
        "nicht",
        "no",
        "nope",
        "cancel",
    }
)
_WORD_RE = re.compile(r"[a-zäöüß]+")

# German action verbs for the confirmation question, by service. Falls back to
# the bare service name so an extension to SENSITIVE_SERVICES still asks.
_ACTION_VERBS: dict[str, str] = {
    "open_cover": "öffnen",
    "open": "öffnen",
    "unlock": "entsperren",
    "alarm_disarm": "entschärfen",
}


def confirm_prompt(domain: str, service: str, entity_id: str) -> str:
    """The "Soll ich … wirklich …?" question for a held sensitive action.

    Uses the entity_id's readable slug (no HA round-trip — the gate must be
    synchronous and deterministic); the model relays it verbatim."""
    name = (
        entity_id.split(".", 1)[1].replace("_", " ") if "." in entity_id else entity_id
    )
    verb = _ACTION_VERBS.get(service, service)
    return f"Soll ich {name} wirklich {verb}?"


def is_sensitive(domain: str, service: str) -> bool:
    """True when a ha_call_service domain+service can open/unsecure the house."""
    return domain in SENSITIVE_DOMAINS or service in SENSITIVE_SERVICES


def is_affirmative(text: str) -> bool:
    words = set(_WORD_RE.findall((text or "").lower()))
    # Negative wins on a tie ("nein doch nicht öffnen") — never auto-execute on
    # an ambiguous reply; only a clean yes proceeds.
    if words & _NEGATIVE:
        return False
    return bool(words & _AFFIRMATIVE)


def is_negative(text: str) -> bool:
    words = set(_WORD_RE.findall((text or "").lower()))
    return bool(words & _NEGATIVE)


@dataclass
class PendingAction:
    """A sensitive ha_call_service held for confirmation."""

    domain: str
    service: str
    entity_id: str
    data: dict[str, Any] | None
    prompt: str

    def args(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "domain": self.domain,
            "service": self.service,
            "entity_id": self.entity_id,
        }
        if self.data:
            out["data"] = self.data
        return out


class PendingStore:
    """Per-session stash of one pending sensitive action.

    In-memory and keyed by the session/source id the loop runs under — both the
    durable household session (voice + browser share one row) and the stateless
    facade source land here, so the gate survives the turn boundary without a
    schema change. One slot per session: a fresh sensitive request replaces an
    unanswered one.
    """

    def __init__(self) -> None:
        self._pending: dict[str, PendingAction] = {}

    def stash(self, session_id: str, action: PendingAction) -> None:
        self._pending[session_id] = action

    def take(self, session_id: str) -> PendingAction | None:
        return self._pending.pop(session_id, None)

    def peek(self, session_id: str) -> PendingAction | None:
        return self._pending.get(session_id)

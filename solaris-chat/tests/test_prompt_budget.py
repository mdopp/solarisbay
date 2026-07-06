"""Assembled household prompt stays within the ≤3k-token budget (#643).

The stated budget lives in profiles.py:4. The static core the household soul
contributes — the shipped SOUL.md plus the pinned-last _TOOL_DISCIPLINE — is
asserted here under a conservative ~4-chars/token estimate (the same convention
as store.truncate_session_head). A ~1000-token allowance is left for the dynamic
entity-registry block + identity that _system_prompt() appends at runtime, inside
the 3k ceiling. German tokenizes heavier than chars/4, so the box /verify check
of the real assembled `prompt_tokens` is the backstop.

Must NOT import alembic (it lives only in database/; CI's solaris-chat env has
none — #378).
"""

from __future__ import annotations

from pathlib import Path

from solaris_chat.engine.client import _TOOL_DISCIPLINE

_STATIC_CORE_BUDGET_TOKENS = 2000


def _shipped_soul() -> str:
    pack = (
        Path(__file__).resolve().parents[2]
        / "templates"
        / "solaris"
        / "skills"
        / "household"
    )
    return (pack / "SOUL.md").read_text(encoding="utf-8")


def test_household_static_prompt_within_budget():
    soul_est = len(_shipped_soul()) // 4
    discipline_est = len(_TOOL_DISCIPLINE) // 4
    total = soul_est + discipline_est
    assert total <= _STATIC_CORE_BUDGET_TOKENS, (
        f"soul {soul_est} + discipline {discipline_est} = {total} est-tok "
        f"> {_STATIC_CORE_BUDGET_TOKENS} (leaves <1k for registry+identity in 3k)"
    )

"""Shared lightly-fuzzy scorer — the proven music-resolve blend (#588, #591).

Extracted verbatim from `engine/tools/music_query.py` so retrieval paths beyond
the music library (notes search) reuse the same token-containment + per-token
edit-ratio blend instead of re-deriving a scorer. stdlib `difflib` only.

Three signals blend, weighted: (a) WHOLE-WORD containment — a query token is a
whole word in the candidate ('joel' in 'Billy Joel', 'queens' in 'Queens of the
Stone Age'); this dominates. (b) the best per-token edit-ratio against the
candidate's words catches typos ('Beatls' → 'Beatles'). (c) a small full-string
ratio + prefix bonus break near-ties. A score must clear `FUZZY_THRESHOLD` to
count as a match rather than a random pick.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher

_FUZZY_WORD_WEIGHT = 0.45
_FUZZY_TOKEN_WEIGHT = 0.45
_FUZZY_FULL_WEIGHT = 0.1
_FUZZY_PREFIX_BONUS = 0.05
FUZZY_THRESHOLD = 0.45

_WORD_RE = re.compile(r"\w+", re.UNICODE)


def tokens(text: str) -> list[str]:
    return _WORD_RE.findall(text.lower())


def fuzzy_score(query: str, candidate: str) -> float:
    q_tokens = tokens(query)
    c_tokens = tokens(candidate)
    if not q_tokens or not c_tokens:
        return 0.0
    c_set = set(c_tokens)
    word_frac = sum(1 for t in q_tokens if t in c_set) / len(q_tokens)
    per_token = sum(
        max(SequenceMatcher(None, t, w).ratio() for w in c_tokens) for t in q_tokens
    ) / len(q_tokens)
    full = SequenceMatcher(None, query.lower(), candidate.lower()).ratio()
    prefix = _FUZZY_PREFIX_BONUS if candidate.lower().startswith(query.lower()) else 0.0
    return (
        _FUZZY_WORD_WEIGHT * word_frac
        + _FUZZY_TOKEN_WEIGHT * per_token
        + _FUZZY_FULL_WEIGHT * full
        + prefix
    )

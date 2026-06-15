"""The OKF safe-slug rule (docs/okf-write-contract.md §2).

Slug = lowercase, digits, dashes only — no `/`, no `..`, no leading dots, no
whitespace. A path component derived from arbitrary ingested text must never be
able to escape the `notes/okf/<domain>/` subtree.
"""

from __future__ import annotations

import re
import unicodedata


_GERMAN = {"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"}


def safe_slug(text: str) -> str:
    """Return an OKF-safe slug, or raise ``ValueError`` if nothing survives.

    Folds common German letters, strips accents, lowercases, then collapses
    every run of non `[a-z0-9]` to a single dash. The result can only contain
    lowercase letters, digits and dashes, so `/`, `..`, leading dots and
    whitespace are structurally impossible.
    """
    lowered = text.strip().lower()
    for src, dst in _GERMAN.items():
        lowered = lowered.replace(src, dst)
    decomposed = unicodedata.normalize("NFKD", lowered)
    ascii_only = decomposed.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_only).strip("-")
    if not slug:
        raise ValueError(f"text produces an empty slug: {text!r}")
    return slug

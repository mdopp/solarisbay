"""Shared text normalization for fuzzy track/artist matching.

Both sides of the music comparison (the YouTube Music history and the on-disk
library tags) get funneled through ``normalize`` so cosmetic differences
— casing, diacritics, "(Remastered)", "feat. X", punctuation — don't cause a
missed match.
"""

from __future__ import annotations

import re
import unicodedata

_PAREN = re.compile(r"[(\[].*?[)\]]")
_FEAT = re.compile(r"\b(feat|ft|featuring|prod)\b.*", re.IGNORECASE)


def normalize(s: str) -> str:
    s = s or ""
    # German ß and "ss" are written interchangeably (Großstadt vs Grossstadt);
    # fold them so the two spellings match. NFKD then handles ä/ö/ü etc.
    s = s.replace("ß", "ss").replace("ẞ", "ss")
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower().replace("&", " and ")
    s = _PAREN.sub(" ", s)
    s = _FEAT.sub(" ", s)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def track_key(artist: str, title: str) -> str:
    return f"{normalize(artist)}\t{normalize(title)}"

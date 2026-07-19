"""Guess a category (Podcast / Hörspiel / music) the history structure can't reveal.

The primary classifier is an LLM (installed by the music import job via
``set_llm_classifier``): it recognises far more shows than any shipped list and
handles unseen names. The shipped seed lists below stay as the OFFLINE FALLBACK —
used verbatim when no LLM classifier is installed or the LLM is unreachable — so
classification degrades to mechanical rather than failing.

Both paths are best-effort and the user can re-categorise anything. Matching is on
the normalized channel/artist (podcasts are usually the show name) and — for
Hörspiele — also the title, since the franchise name often sits in the episode
title. Short/ambiguous names that would collide with music artists are omitted.
"""

from __future__ import annotations

from collections.abc import Callable

from .textnorm import normalize

# An installed LLM classifier: ``fn(artist, title) -> "Podcast"|"Hörspiel"|None``.
# The music import job installs one (see ``importers.music``); left None it never
# runs and ``classify`` uses the shipped seed lists only.
_LLM_CLASSIFIER: Callable[[str, str], str | None] | None = None


def set_llm_classifier(fn: Callable[[str, str], str | None] | None) -> None:
    """Install (or clear) the LLM-backed classifier ``classify`` prefers."""
    global _LLM_CLASSIFIER
    _LLM_CLASSIFIER = fn


_HOERSPIEL = [
    "Die drei Fragezeichen",
    "Drei Fragezeichen",
    "Die drei ??? Kids",
    "TKKG",
    "Bibi Blocksberg",
    "Benjamin Blümchen",
    "Bibi und Tina",
    "Fünf Freunde",
    "Die drei Ausrufezeichen",
    "Was ist Was",
    "Paw Patrol",
    "Feuerwehrmann Sam",
    "Der kleine Drache Kokosnuss",
    "Die Schule der magischen Tiere",
    "Gregs Tagebuch",
    "Der Räuber Hotzenplotz",
    "Pettersson und Findus",
    "Leo Lausemaus",
    "Sternenschweif",
    "Lauras Stern",
    "Ritter Rost",
    "Yakari",
    "Die Playmos",
    "Anna und die wilden Tiere",
    "Peppa Pig",
    "PJ Masks",
    "Jim Knopf",
    "Der Grüffelo",
    "Pumuckl",
    "Jan Tenner",
    "John Sinclair",
    "Die Teufelskicker",
    "Gruselkabinett",
    "Point Whitmark",
    "Das kleine Gespenst",
]

_PODCAST = [
    "Fest & Flauschig",
    "Gemischtes Hack",
    "Lage der Nation",
    "Baywatch Berlin",
    "Apokalypse & Filterkaffee",
    "Hotel Matze",
    "Alles gesagt",
    "Gefühlte Fakten",
    "Betreutes Fühlen",
    "Herrengedeck",
    "Beste Freundinnen",
    "Doppelgänger Tech",
    "Handelsblatt Today",
    "OMR Podcast",
    "Zeit Verbrechen",
    "Weird Crimes",
    "Mordlust",
    "Verbrechen von nebenan",
    "Mord auf Ex",
    "Hobbylos",
    "Copa TS",
    "Almost Daily",
    "Jung & Naiv",
    "Aufwachen Podcast",
    "Kanackische Welle",
    "Deutschland3000",
    "Sträflich",
    "Dunkle Heimat",
    "Kurt Krömer Feelings",
    "The Joe Rogan Experience",
    "Lex Fridman",
    "Huberman Lab",
    "Hardcore History",
    "Stuff You Should Know",
    "99% Invisible",
    "This American Life",
    "Radiolab",
    "Crime Junkie",
    "My Favorite Murder",
    "SmartLess",
    "Call Her Daddy",
    "The Rest Is History",
    "The Rest Is Politics",
    "The Diary Of A CEO",
    "Darknet Diaries",
    "Reply All",
    "Planet Money",
    "Acquired",
    "Freakonomics",
]

# Normalize once; drop anything too short to safely substring-match.
_H = [n for n in (normalize(x) for x in _HOERSPIEL) if len(n) >= 5]
_P = [n for n in (normalize(x) for x in _PODCAST) if len(n) >= 5]


def classify(artist: str, title: str) -> str | None:
    """Return "Podcast" / "Hörspiel" if the show is recognised, else None.

    Prefers the installed LLM classifier; falls back to the shipped seed lists
    when none is installed or the LLM raised/returned an unusable value."""
    if _LLM_CLASSIFIER is not None:
        try:
            label = _LLM_CLASSIFIER(artist, title)
        except Exception:  # noqa: BLE001 — LLM failure must degrade, not crash.
            label = None
        if label in ("Podcast", "Hörspiel"):
            return label
        if label == "Musik":
            return None
        # None / anything unexpected → fall through to the mechanical seed lists.
    return _classify_mechanical(artist, title)


def _classify_mechanical(artist: str, title: str) -> str | None:
    """The shipped seed-list classifier — the offline fallback."""
    a = normalize(artist)
    hay = f"{a} {normalize(title)}".strip()
    if any(n in a for n in _P):
        return "Podcast"
    if any(n in hay for n in _H):
        return "Hörspiel"
    return None

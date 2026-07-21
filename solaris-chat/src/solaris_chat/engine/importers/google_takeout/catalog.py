"""Guess a category (Podcast / Hörspiel / music) the history structure can't reveal.

Classification is mechanical: a shipped seed list of known shows matched against
the normalized channel/artist (podcasts are usually the show name) and — for
Hörspiele — also the title, since the franchise name often sits in the episode
title. Short/ambiguous names that would collide with music artists are omitted.
Best-effort by design, and the user can re-categorise anything; deterministic and
instant, no network/LLM call in the import hot path.
"""

from __future__ import annotations

from .textnorm import normalize

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
    """Return "Podcast" / "Hörspiel" if a known show is recognised, else None."""
    a = normalize(artist)
    hay = f"{a} {normalize(title)}".strip()
    if any(n in a for n in _P):
        return "Podcast"
    if any(n in hay for n in _H):
        return "Hörspiel"
    return None

"""The normalized record an adapter hands the OKF writer.

Adapters are the only place source-specific shape lives; everything downstream
(OKF serialization, the `.db` projection, embedding, ingest_log) operates on a
`ConceptRecord`. Keeping this dataclass adapter-agnostic is what makes the
writer the single shared write-path core (docs/okf-write-contract.md §6).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# OKF concept types and the domain subdir each lives under (§2/§3).
# `note` is the catch-all for a hand-written vault note the Obsidian adapter
# normalizes when it carries no more specific type (#448).
_DOMAIN_BY_TYPE = {
    "person": "people",
    "event": "events",
    "place": "places",
    "book": "books",
    "song": "songs",
    "album": "albums",
    "band": "bands",
    "trip": "trips",
    "note": "notes",
    # A life-document (insurance, contract, …) extracted from an upload: one
    # `document` type carrying a `category` fact, not a type per category (#doc).
    "document": "documents",
}

# Types projected to the `events` table; everything else is an `entity` (§4).
_EVENT_TYPES = frozenset({"event"})


def is_known_type(concept_type: str) -> bool:
    return concept_type in _DOMAIN_BY_TYPE


def domain_for(concept_type: str) -> str:
    try:
        return _DOMAIN_BY_TYPE[concept_type]
    except KeyError:
        raise ValueError(f"unknown OKF concept type: {concept_type!r}") from None


def is_event_type(concept_type: str) -> bool:
    return concept_type in _EVENT_TYPES


def fact_triple(
    fact: tuple[str, str] | tuple[str, str, float | None],
) -> tuple[str, str, float | None]:
    """Normalize a `ConceptRecord.facts` entry to `(predicate, value, confidence)`.

    A 2-tuple (the common case) carries no confidence → None; a 3-tuple supplies
    it (#881, ADR 0003)."""
    if len(fact) == 3:
        return fact[0], fact[1], fact[2]
    return fact[0], fact[1], None


@dataclass(frozen=True)
class Relationship:
    """One `## Relationships` line: ``- <rel> → [[<path>]]`` (§3).

    `rel` projects to `event_entities.role` (for events) or `facts.predicate`
    (for entities); `path` is the OKF target link the consumer follows.
    """

    rel: str
    path: str


@dataclass
class ConceptRecord:
    """A source-agnostic concept ready to be written.

    Required: `type`, `title`, `source` (``<adapter>:<external_id>``).
    `slug` is derived from `title` when omitted. `resident` defaults to the
    ingesting resident at the writer boundary, so an adapter that doesn't know
    the uid can leave it empty.
    """

    type: str
    title: str
    source: str
    external_id: str
    slug: str = ""
    resident: str = ""
    description: str = ""
    body: str = ""
    timestamp: str = ""
    resource: str = ""
    tags: list[str] = field(default_factory=list)
    # Type-specific frontmatter (when/where/participants/author/...). Rendered
    # verbatim into the frontmatter; the writer never invents these keys.
    extra: dict[str, Any] = field(default_factory=dict)
    aliases: list[str] = field(default_factory=list)
    relationships: list[Relationship] = field(default_factory=list)
    # Free-text attribute facts (predicate, value) projected to `facts` and
    # rendered into frontmatter — for non-link facts a `## Relationships` link
    # can't carry (a band's genre / bio). A fact may carry an optional third
    # element, a confidence in [0,1] (ADR 0003 provenance & trust — a chat-derived
    # `used_to_love` is softer than an external edge, #881); omitting it means
    # None (a fact stated as certain).
    facts: list[tuple[str, str] | tuple[str, str, float | None]] = field(
        default_factory=list
    )
    # event-only: ISO timestamp + kind for the `events` row.
    event_ts: str = ""
    event_kind: str = ""
    # Provenance policy (ADR 0002/0005): an externally re-ingestable per-item
    # concept (a Jellyfin song) lives ONLY as a projection row + facts — no OKF
    # markdown, no whole-concept embedding. The RAG-worthy album/artist nodes
    # keep both. Set by the adapter that owns the source's provenance.
    projection_only: bool = False


@dataclass(frozen=True)
class WriteResult:
    """What `write_concept` returns to the adapter."""

    concept_id: str
    ref_id: str
    ref_kind: str
    okf_path: str
    content_hash: str
    skipped: bool  # True when an unchanged re-ingest short-circuited.
    embedded: bool  # True when an embedding (re-)enqueue happened.

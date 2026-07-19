"""Chat-derived music affinity → `used_to_love` album facts (#881, B9, ADR 0003).

The nightly Stenograph distils a day's conversation into durable facts. When a
resident says they *used to love* an album ("das war früher mein Lieblingsalbum",
"das Album hab ich rauf und runter gehört"), that affinity must become a
QUERYABLE signal on the **album entity**, not just free-text holographic memory:
a `used_to_love` fact (source=stenograph) so the P2a wishlist query surfaces a
chat-loved album a resident neither owns physically nor has digitally (you can
love something you neither own nor have digital — the album entity is created if
Jellyfin/the vault never saw it, mirroring P2b's physical-media path).

Detection is deterministic (a trigger-phrase set, like `remember.py`), NOT an
LLM turn: the queryable fact must land reliably, on the right album entity,
idempotently — a discretionary "please also call a tool" instruction to a small
model does not clear that bar. The affinity is phrased "«Album» von «Artist»";
we extract that (artist, album) pair and route it exactly as P2b routes a
physical-media note, but source-tagged `stenograph` at a softer confidence
(provenance & trust — a chat mention is weaker than a Jellyfin `on_album` edge).

When the same turn carries a memory/story ("… weil wir das immer im Auto vom
Urlaub gehört haben"), a self-originated **Musik-Erinnerung** note is written too
(markdown = narrative context, ADR 0002) — but the queryable signal is the fact.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .knowledge import ConceptRecord, projection, safe_slug
from .knowledge.writer import OkfWriter

# Source tag for a chat-derived album fact (ADR 0003 — one entity, many sources:
# it coexists with Jellyfin's `by`/`on_album` and a note's `owned_physical`).
_SOURCE = "stenograph"

# A chat mention is softer than an external has_digital/on_album edge (ADR 0003
# provenance & trust): the fact is written at this confidence, not 1.0.
_CONFIDENCE = 0.5

# The affinity openers — a PAST-love statement about an album. Deliberately
# narrow (past tense + a love/heavy-rotation verb) so a neutral "ich höre X von
# Y" (present, not nostalgia) never triggers. Each captures the album title (in
# quotes or up to the ` von `) and the artist after ` von `. German + a little
# English; case-insensitive.
_ALBUM = r"[\"„»']?(?P<album>[^\"“«'\n]+?)[\"“«']?"
_ARTIST = r"(?P<artist>[^\n.,;!?]+?)"
# An optional temporal adverb ("früher"/"damals") that may sit before the album.
_ADV = r"(?:(?:fr(?:ü|ue)her|damals)\s+)?"
# "ich habe" (subject-verb) or "habe ich" (verb-subject inversion after an adverb).
_ICH_HABE = r"(?:ich\s+hab(?:e)?|hab(?:e)?\s+ich)"
_TAIL = r"\s*(?:[.,;!?].*)?$"
_PATTERNS: tuple[re.Pattern[str], ...] = (
    # "… (früher) mein Lieblingsalbum war (früher) «Album» von «Artist»"
    re.compile(
        r"lieblingsalbum\b.*?\bwar\b\s+"
        + _ADV
        + _ALBUM
        + r"\s+von\s+"
        + _ARTIST
        + _TAIL,
        re.IGNORECASE,
    ),
    # "«Album» von «Artist» war (früher) mein Lieblingsalbum"
    re.compile(
        _ALBUM + r"\s+von\s+" + _ARTIST + r"\s+war\b.*?\blieblingsalbum",
        re.IGNORECASE,
    ),
    # "«Album» von «Artist» hab(e) ich (früher) rauf und runter gehört"
    re.compile(
        _ALBUM
        + r"\s+von\s+"
        + _ARTIST
        + r"\s+hab(?:e)?\s+ich\b.*?\brauf und runter\b.*?geh(?:ö|oe)rt",
        re.IGNORECASE,
    ),
    # "(früher) (ich) hab(e) (ich) (früher) «Album» von «Artist» rauf und runter gehört"
    re.compile(
        r"\b"
        + _ICH_HABE
        + r"\s+"
        + _ADV
        + _ALBUM
        + r"\s+von\s+"
        + _ARTIST
        + r"\s+rauf und runter\b.*?geh(?:ö|oe)rt"
        + _TAIL,
        re.IGNORECASE,
    ),
    # A past-love "geliebt" statement in either word order, an optional adverb
    # before/after: "(ich) hab(e) (ich) (früher) «Album» von «Artist» (früher) geliebt".
    re.compile(
        r"\b"
        + _ICH_HABE
        + r"\s+"
        + _ADV
        + _ALBUM
        + r"\s+von\s+"
        + _ARTIST
        + r"\s+"
        + _ADV
        + r"geliebt"
        + _TAIL,
        re.IGNORECASE,
    ),
)


@dataclass(frozen=True)
class Affinity:
    """One past-music-love statement extracted from a chat turn."""

    artist: str
    album: str
    # The whole user turn, kept as the Musik-Erinnerung note body when it carries
    # narrative beyond the bare "used to love" (a memory/story worth the note).
    memory: str


def extract_affinities(msgs: list[tuple[str, str]]) -> list[Affinity]:
    """The past-music-love affinities in a day's `(role, content)` slice.

    Only USER turns are scanned (a resident's own statement, not Solaris's
    paraphrase). Deduped on (artist, album) casefold so a repeated mention in one
    day yields one affinity; order preserved (first mention wins the memory)."""
    out: list[Affinity] = []
    seen: set[tuple[str, str]] = set()
    for role, content in msgs:
        if role != "user" or not content:
            continue
        for pattern in _PATTERNS:
            m = pattern.search(content)
            if m is None:
                continue
            artist = m.group("artist").strip(" \t\n\"“«»'.,;!?")
            album = m.group("album").strip(" \t\n\"“«»'.,;!?")
            if not artist or not album:
                continue
            key = (artist.casefold(), album.casefold())
            if key in seen:
                continue
            seen.add(key)
            out.append(Affinity(artist=artist, album=album, memory=content.strip()))
    return out


# A memory note is written only when the turn carries narrative beyond the bare
# affinity trigger — a rough proxy: a "weil/because/wir/damals/immer" clause.
_HAS_MEMORY_RE = re.compile(
    r"\b(?:weil|because|damals|immer|erinner|urlaub|auto|wir\b)", re.IGNORECASE
)


def route_affinities(
    writer: OkfWriter,
    db_path: str,
    owner_uid: str,
    affinities: list[Affinity],
) -> int:
    """Write each affinity as a `used_to_love` album fact (source=stenograph).

    Resolves/creates the album entity by P1a's "Artist – Album" canonical_name +
    `{artist_slug}-{album_slug}` slug (create-if-absent — you can love an album
    you neither own nor have digitally), projection_only from the chat side so it
    never collides with Jellyfin's/the note's album file. The source-scoped
    fact-replace (#880) keeps a Jellyfin `by` edge or a note's `owned_physical`
    intact. When the turn carries a memory, a self-originated Musik-Erinnerung
    note is written too (narrative context, ADR 0002). Per-resident (owner_uid);
    idempotent (the writer's ingest_log short-circuits an unchanged re-run).

    Returns the number of album facts written/updated."""
    written = 0
    for aff in affinities:
        try:
            artist_slug = safe_slug(aff.artist)
            album_slug = safe_slug(aff.album)
        except ValueError:
            # Nothing slug-able (punctuation-only) — skip, don't crash the run.
            continue
        album_rec = ConceptRecord(
            type="album",
            title=f"{aff.artist} – {aff.album}",
            slug=f"{artist_slug}-{album_slug}",
            source=_SOURCE,
            external_id=f"stenograph:{owner_uid}:{artist_slug}-{album_slug}",
            resident=owner_uid,
            facts=[("used_to_love", "", _CONFIDENCE)],
            projection_only=True,
        )
        if not writer.write_concept(album_rec, ingesting_uid=owner_uid).skipped:
            written += 1
        if _HAS_MEMORY_RE.search(aff.memory):
            _write_memory_note(writer, owner_uid, aff, artist_slug, album_slug)
    return written


def _write_memory_note(
    writer: OkfWriter,
    owner_uid: str,
    aff: Affinity,
    artist_slug: str,
    album_slug: str,
) -> None:
    """A self-originated Musik-Erinnerung note (markdown = narrative, ADR 0002)
    linked to the album by the same slug, so it rides RAG next to the fact."""
    note_rec = ConceptRecord(
        type="note",
        title=f"Musik-Erinnerung: {aff.artist} – {aff.album}",
        slug=f"musik-erinnerung-{artist_slug}-{album_slug}",
        source=_SOURCE,
        external_id=f"stenograph-memory:{owner_uid}:{artist_slug}-{album_slug}",
        resident=owner_uid,
        body=aff.memory,
        relationships=[_album_link(artist_slug, album_slug)],
    )
    writer.write_concept(note_rec, ingesting_uid=owner_uid)


def _album_link(artist_slug: str, album_slug: str):
    from .knowledge import Relationship

    return Relationship("related", f"albums/{artist_slug}-{album_slug}")


def album_used_to_love(db_path: str, owner_uid: str) -> list[str]:
    """Canonical names of this resident's chat-derived used_to_love albums.

    A small read helper for tests/verification — the runtime signal is the
    `used_to_love` fact the P2a wishlist query already reads."""
    conn = projection.open_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT DISTINCT e.canonical_name FROM entities e"
            " JOIN facts f ON f.subject_entity_id = e.id"
            " WHERE e.type = 'album' AND f.predicate = 'used_to_love'"
            " AND f.source = ? AND e.resident_uid = ? ORDER BY e.canonical_name",
            (_SOURCE, owner_uid),
        ).fetchall()
        return [r["canonical_name"] for r in rows]
    finally:
        conn.close()

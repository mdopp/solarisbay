"""Obsidian ingest adapter (#448, docs/okf-write-contract.md §6).

Normalizes the household's existing **hand-written** Obsidian vault notes into
OKF concepts via the shared #447 writer — establishing OKF conformance for
hand-authored knowledge **without destroying the originals**: the source vault
is read-only and the OKF output goes under the separate `notes/okf/` subtree
(§2).

Each note becomes one OKF concept:

  - **type** from the note's frontmatter `type`, else a known top-level folder
    name (`people`→person, `events`→event, …), else the catch-all `note`;
  - `title`/`tags`/`timestamp` carried over from existing frontmatter where
    present; `source = obsidian:<relpath>`; `resident` = the ingesting resident
    (the writer default);
  - the note **body is preserved**; existing `[[wikilinks]]` that point at an
    already-written OKF concept become `## Relationships` `related → [[…]]`
    edges, while links to unknown targets are left as plain links in the body.

Idempotent + incremental: every write goes through the writer's `ingest_log`
(`source="obsidian"`, the relpath external_id) + `content_hash`, so a re-run
only touches changed notes.
"""

from __future__ import annotations

from dataclasses import dataclass

from ... import notes_search
from ...logging import log
from ..knowledge import ConceptRecord, Relationship, projection, safe_slug
from ..knowledge.records import domain_for, is_known_type
from ..knowledge.writer import OkfWriter
from .obsidian_reader import ObsidianReader, VaultNote


_SOURCE = "obsidian"
# A physical-collection note's facts are attributed to the resident, not to the
# vault reader, so they merge with a Jellyfin `by` edge on the same album entity
# (ADR 0003) and the wishlist query counts an `owned_physical` album as "have it
# physically → don't buy" (#880, ADR 0002/0005).
_NOTE_FACT_SOURCE = "note"
_PHYSICAL_MEDIA_TYPE = "physical-media"
_PHYSICAL_MEDIA = frozenset({"cd", "vinyl", "cassette"})

# A life-document note the extraction agent writes from an uploaded file (#doc):
# `type: document` + flat frontmatter fields become source-scoped facts at
# extraction confidence, so a human correction (source `documents:confirmed`,
# confidence 1.0) wins without being clobbered (ADR 0003).
_DOCUMENT_TYPE = "document"
_DOCUMENT_FACT_SOURCE = "documents"
_DOCUMENT_CONFIDENCE = 0.6
# Frontmatter keys that are note metadata, not extracted document fields.
_DOCUMENT_RESERVED_FM = frozenset(
    {
        "type",
        "title",
        "tags",
        "timestamp",
        "id",
        "resident",
        "source",
        "added_by",
        "date",
        "kind",
        "description",
    }
)

# A hand-written note's top-level folder hints at its OKF type when the note
# carries no explicit `type` frontmatter.
_FOLDER_TYPE = {
    "people": "person",
    "person": "person",
    "events": "event",
    "event": "event",
    "places": "place",
    "place": "place",
    "books": "book",
    "songs": "song",
    "bands": "band",
    "trips": "trip",
}


@dataclass
class ObsidianIngestStats:
    notes: int = 0
    written: int = 0
    skipped: int = 0


class ObsidianIngest:
    def __init__(
        self,
        reader: ObsidianReader,
        writer: OkfWriter,
        *,
        db_path: str,
        ingesting_uid: str,
    ):
        self._reader = reader
        self._writer = writer
        # The adapter resolves `[[wikilinks]]` against the OKF projection, so it
        # needs the same db the writer projects into (read-only here).
        self._db_path = db_path
        self._uid = ingesting_uid

    def run(self) -> ObsidianIngestStats:
        """Normalize every hand-written vault note into an OKF concept."""
        stats = ObsidianIngestStats()
        for note in self._reader.iter_notes():
            try:
                self._ingest_note(note, stats)
            except Exception as e:
                # One malformed note must never abort the whole vault ingest.
                log.error(
                    "engine.ingest.obsidian_note_failed",
                    relpath=note.relpath,
                    error=str(e),
                )
                stats.skipped += 1
            stats.notes += 1
        return stats

    def _ingest_note(self, note: VaultNote, stats: ObsidianIngestStats) -> None:
        if note.note_type == _PHYSICAL_MEDIA_TYPE:
            self._ingest_physical_media(note, stats)
            return
        if note.note_type == _DOCUMENT_TYPE:
            self._ingest_document(note, stats)
            return
        concept_type = note.note_type or _FOLDER_TYPE.get(note.folder, "note")
        if not is_known_type(concept_type):
            # An explicit frontmatter type the OKF model has no domain for (e.g.
            # `journal` diary entries) is not an OKF concept — skip, don't crash.
            log.info(
                "engine.ingest.obsidian_note_skipped",
                relpath=note.relpath,
                concept_type=concept_type,
            )
            stats.skipped += 1
            return
        rels, body = self._relationships(note)
        # A hand-written note under `users/<uid>/` is private to that resident —
        # scope its structured projection to the same owner the read-side derives
        # from the path (#576), so it can't leak as `household` via concept reads.
        # No path match → "" → writer default (household).
        rec = ConceptRecord(
            type=concept_type,
            title=note.title,
            source=_SOURCE,
            external_id=note.relpath,
            resident=notes_search.resident_for_path(note.relpath) or "",
            timestamp=note.timestamp,
            tags=note.tags,
            body=body,
            relationships=rels,
        )
        if self._writer.write_concept(rec, ingesting_uid=self._uid).skipped:
            stats.skipped += 1
        else:
            stats.written += 1

    def _ingest_physical_media(
        self, note: VaultNote, stats: ObsidianIngestStats
    ) -> None:
        """A physical-collection note (records/LPs/cassettes owned in the real
        world, #880): self-originated markdown = source of truth with the user's
        own sleeve photo the only image (ADR 0002/0005). It converges on the
        matching **album entity** (P1a's "Artist – Album" canonical_name / slug),
        creating it if Jellyfin never saw it (an owned-physical album with no
        digital presence — the core digitize case), and contributes source-tagged
        `owned_physical`/(`used_to_love`)/`digitize` facts (source=note, ADR 0003)
        to it. The album carries only facts here — no album markdown/embedding —
        so it never collides with Jellyfin's lean album file; the note keeps its
        own markdown + embedding (the RAG surface, sleeve photo intact).
        """
        fm = note.frontmatter
        artist = fm.get("artist", "").strip()
        album = fm.get("album", "").strip()
        medium = fm.get("medium", "").strip().casefold()
        if not artist or not album or medium not in _PHYSICAL_MEDIA:
            # An incomplete note (no artist/album, or an unknown medium) can't
            # attach to an album entity — skip it, don't crash the run.
            log.info(
                "engine.ingest.physical_media_skipped",
                relpath=note.relpath,
                medium=medium,
            )
            stats.skipped += 1
            return
        resident = notes_search.resident_for_path(note.relpath) or ""
        # The note itself: self-originated `note` concept, markdown = truth +
        # embedding, the sleeve photo (`![[...]]` in the body) the only image.
        note_rec = ConceptRecord(
            type="note",
            title=note.title,
            source=_SOURCE,
            external_id=note.relpath,
            resident=resident,
            timestamp=note.timestamp,
            tags=note.tags,
            body=note.body,
        )
        note_written = not self._writer.write_concept(
            note_rec, ingesting_uid=self._uid
        ).skipped
        # The album entity: resolve/create by (artist, album) via P1a's
        # canonical_name + slug so it merges with the Jellyfin album, and attach
        # note-sourced facts. Projection-only from the note side (no album
        # markdown/embedding — Jellyfin owns those); source-scoped fact-replace
        # keeps a Jellyfin `by` edge intact.
        artist_slug = safe_slug(artist)
        album_slug = safe_slug(album)
        facts: list[tuple[str, str]] = [("owned_physical", medium)]
        if _truthy(fm.get("used_to_love", "")):
            facts.append(("used_to_love", ""))
        digitize = fm.get("digitize", "").strip().casefold()
        if digitize in ("todo", "done"):
            facts.append(("digitize", digitize))
        source = fm.get("source", "").strip()
        if source:
            facts.append(("source", source))
        album_rec = ConceptRecord(
            type="album",
            title=f"{artist} – {album}",
            slug=f"{artist_slug}-{album_slug}",
            source=_NOTE_FACT_SOURCE,
            external_id=f"physical-media:{note.relpath}",
            resident=resident,
            facts=facts,
            projection_only=True,
        )
        album_written = not self._writer.write_concept(
            album_rec, ingesting_uid=self._uid
        ).skipped
        if note_written or album_written:
            stats.written += 1
        else:
            stats.skipped += 1

    def _ingest_document(self, note: VaultNote, stats: ObsidianIngestStats) -> None:
        """A `type: document` note (an insurance/contract/… the extraction agent
        wrote from an uploaded file): every non-reserved flat frontmatter field
        (`category`, `provider`, `policy_number`, `cancellation_deadline`,
        `source_document`, …) becomes a source-scoped fact at extraction
        confidence (0.6) on a `document` entity. The category view tables these;
        a human correction under source `documents:confirmed` (confidence 1.0)
        wins without being clobbered (ADR 0003). The note keeps its own markdown
        + embedding (the RAG surface). Slug is pinned to the file stem so the
        writer re-serializes the agent's file in place instead of forking a
        canonical copy."""
        facts = [
            (key, str(val).strip(), _DOCUMENT_CONFIDENCE)
            for key, val in note.frontmatter.items()
            if key not in _DOCUMENT_RESERVED_FM and str(val).strip()
        ]
        rec = ConceptRecord(
            type=_DOCUMENT_TYPE,
            title=note.title,
            slug=note.relpath.rsplit("/", 1)[-1].removesuffix(".md"),
            source=_DOCUMENT_FACT_SOURCE,
            external_id=note.relpath,
            resident=notes_search.resident_for_path(note.relpath) or "",
            timestamp=note.timestamp,
            tags=note.tags,
            body=note.body,
            facts=facts,
        )
        if self._writer.write_concept(rec, ingesting_uid=self._uid).skipped:
            stats.skipped += 1
        else:
            stats.written += 1

    def _relationships(self, note: VaultNote) -> tuple[list[Relationship], str]:
        """Convert `[[wikilinks]]` that resolve to an already-written OKF concept
        into `related → [[…]]` edges; leave links to unknown targets as the plain
        body links they already are (the note body is preserved verbatim)."""
        if not note.wikilinks:
            return [], note.body
        conn = projection.open_conn(self._db_path)
        try:
            rels = []
            for target in note.wikilinks:
                okf_path = self._known_okf_path(conn, target)
                if okf_path is not None:
                    rels.append(Relationship("related", okf_path))
        finally:
            conn.close()
        return rels, note.body

    def _known_okf_path(self, conn, target: str) -> str | None:
        """The OKF link path for a wikilink target that names a known concept,
        or None when nothing in the vault matches (left as a plain link)."""
        # A vault link can already be an OKF path (`people/anna`) or a bare name
        # (`Anna`); try the path form first, then a name match across domains.
        if projection.entity_id_for_okf_path(conn, target) is not None:
            return target if "/" in target else None
        slug = _safe_slug_or_none(target)
        if slug is None:
            return None
        for domain in dict.fromkeys(domain_for(t) for t in _FOLDER_TYPE.values()):
            candidate = f"{domain}/{slug}"
            if projection.entity_id_for_okf_path(conn, candidate) is not None:
                return candidate
        return None


def _truthy(value: str) -> bool:
    return value.strip().casefold() in ("true", "yes", "1")


def _safe_slug_or_none(text: str) -> str | None:
    try:
        return safe_slug(text)
    except ValueError:
        return None

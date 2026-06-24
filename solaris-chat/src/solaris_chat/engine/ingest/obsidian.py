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


def _safe_slug_or_none(text: str) -> str | None:
    try:
        return safe_slug(text)
    except ValueError:
        return None

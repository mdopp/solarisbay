"""Immich ingest adapter (#206, docs/okf-write-contract.md §6).

Reads the household Immich library **read-only** and writes OKF concepts via the
shared #447 writer. One asset becomes up to three kinds of concept:

  - **event** — the photo itself: `when` from EXIF, `where` linking the place,
    `media[]`/`resource` = the Immich asset URI, `participants` = the depicted
    people (a `depicted → [[people/…]]` relationship edge per face);
  - **person** — one per Immich-identified, named face on the asset (Immich has
    already done the face → person identification; this adapter only reads that
    metadata, no biometric processing);
  - **place** — when the asset carries EXIF geo, a place concept from the
    lat/lon (+ city/state/country) the event links to.

Scope (§6): default is the configured ingesting resident; an asset Immich shares
with other residents is written as `household` so it's visible cross-resident
(sharing is an Immich fact, not a writer default).

Idempotent + incremental: every write goes through the writer's `ingest_log`
(`source="immich"`, the asset/person/place external_id) + `content_hash`, so a
re-run with an unchanged checksum is a no-op. `iter_assets(updated_after=...)`
uses a persisted sync cursor so re-runs only pull new/changed assets.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from ...logging import log
from ..knowledge import ConceptRecord, Relationship, safe_slug
from ..knowledge.writer import OkfWriter
from .immich_client import ImmichAsset, ImmichClient, ImmichPerson


_SOURCE = "immich"


@dataclass
class ImmichIngestStats:
    assets: int = 0
    events_written: int = 0
    people_written: int = 0
    places_written: int = 0
    skipped: int = 0
    # High-water sync cursor (max asset `when`) for the next incremental run.
    cursor: str = ""


class ImmichIngest:
    def __init__(
        self,
        client: ImmichClient,
        writer: OkfWriter,
        *,
        ingesting_uid: str,
    ):
        self._client = client
        self._writer = writer
        self._uid = ingesting_uid

    # Checkpoint the high-water cursor every N assets so a mid-run disconnect
    # still advances it and the next boot RESUMES instead of re-paging all ~76k
    # assets from page 1 (#597).
    _CHECKPOINT_EVERY = 250

    async def run(
        self,
        *,
        updated_after: str = "",
        checkpoint: Callable[[str], None] | None = None,
    ) -> ImmichIngestStats:
        """Ingest every asset since `updated_after`; return run stats.

        The caller persists the returned high-water cursor (max asset `when`)
        so the next run only pulls newer assets — incremental on top of the
        per-asset content_hash idempotency.

        If a `checkpoint` callback is given it is invoked with the current
        high-water cursor every `_CHECKPOINT_EVERY` assets, so a mid-run abort
        (e.g. a transport disconnect that exhausts the page retries) still
        persists progress and the next boot resumes from there.
        """
        stats = ImmichIngestStats(cursor=updated_after)
        last_checkpointed = stats.cursor
        async for asset in self._client.iter_assets(updated_after=updated_after):
            try:
                self._ingest_asset(asset, stats)
            except Exception as e:  # noqa: BLE001
                # One bad asset (e.g. a non-Latin name -> safe_slug ValueError)
                # must never abort the whole run (#528).
                log.error(
                    "engine.ingest.immich_asset_failed",
                    asset_id=asset.id,
                    error=str(e),
                )
                stats.skipped += 1
            else:
                if asset.when > stats.cursor:
                    stats.cursor = asset.when
            stats.assets += 1
            if (
                checkpoint is not None
                and stats.assets % self._CHECKPOINT_EVERY == 0
                and stats.cursor != last_checkpointed
            ):
                checkpoint(stats.cursor)
                last_checkpointed = stats.cursor
        return stats

    def _ingest_asset(self, asset: ImmichAsset, stats: ImmichIngestStats) -> None:
        scope = self._scope(asset)

        # People first: the event's `depicted` edge resolves to these by their
        # OKF link path, so the person concepts must exist before the event.
        participants: list[Relationship] = []
        for person in asset.people:
            self._write_person(person, scope, stats)
            participants.append(
                Relationship("depicted", f"people/{safe_slug(person.name)}")
            )

        place_rel: list[Relationship] = []
        place_label = self._place_label(asset)
        if asset.latitude is not None and asset.longitude is not None:
            self._write_place(asset, place_label, scope, stats)
            place_rel.append(Relationship("at", f"places/{safe_slug(place_label)}"))

        self._write_event(asset, scope, participants + place_rel, place_label, stats)

    def _write_person(
        self, person: ImmichPerson, scope: str, stats: ImmichIngestStats
    ) -> None:
        rec = ConceptRecord(
            type="person",
            title=person.name,
            source=_SOURCE,
            external_id=f"person/{person.id}",
            resident=scope,
            resource=f"immich:person/{person.id}",
        )
        if not self._writer.write_concept(rec, ingesting_uid=self._uid).skipped:
            stats.people_written += 1

    def _write_place(
        self, asset: ImmichAsset, label: str, scope: str, stats: ImmichIngestStats
    ) -> None:
        # One place per coordinate (rounded) — re-used across assets at the same
        # spot. external_id is the geo key, not the asset, so distinct photos at
        # one place dedup to a single place concept.
        geo = f"{asset.latitude:.4f},{asset.longitude:.4f}"
        rec = ConceptRecord(
            type="place",
            title=label,
            source=_SOURCE,
            external_id=f"place/{geo}",
            resident=scope,
            extra={
                "geo": geo,
                **({"address": label} if label else {}),
            },
        )
        if not self._writer.write_concept(rec, ingesting_uid=self._uid).skipped:
            stats.places_written += 1

    def _write_event(
        self,
        asset: ImmichAsset,
        scope: str,
        rels: list[Relationship],
        place_label: str,
        stats: ImmichIngestStats,
    ) -> None:
        uri = self._client.asset_uri(asset.id)
        title = asset.file_name or f"Photo {asset.id}"
        extra: dict[str, object] = {"media": [uri]}
        if place_label:
            extra["where"] = f"places/{safe_slug(place_label)}"
        rec = ConceptRecord(
            type="event",
            title=title,
            # Suffix the asset id so two photos with the same filename+date
            # don't collide on the date-prefixed event slug (the event dedup
            # key is the OKF path); re-ingest of the same asset is still stable.
            slug=f"{safe_slug(title)}-{safe_slug(asset.id)}",
            source=_SOURCE,
            external_id=f"asset/{asset.id}",
            resident=scope,
            resource=uri,
            timestamp=asset.when,
            event_ts=asset.when,
            event_kind="photo",
            # Immich's content checksum rides the body so a changed asset (new
            # faces, re-geotag) moves the content_hash and re-ingests.
            body=f"Immich asset {asset.id} (checksum {asset.checksum}).",
            extra=extra,
            relationships=rels,
        )
        if self._writer.write_concept(rec, ingesting_uid=self._uid).skipped:
            stats.skipped += 1
        else:
            stats.events_written += 1

    def _scope(self, asset: ImmichAsset) -> str:
        # A shared asset is visible to several residents → household scope (§6);
        # otherwise the writer defaults it to the ingesting resident.
        return "household" if asset.shared_with else ""

    def _place_label(self, asset: ImmichAsset) -> str:
        parts = [p for p in (asset.city, asset.state, asset.country) if p]
        if parts:
            return ", ".join(parts)
        if asset.latitude is not None and asset.longitude is not None:
            return f"{asset.latitude:.4f},{asset.longitude:.4f}"
        return ""

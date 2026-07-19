"""Google-Takeout import core, vendored from ``mdopp/solaris-import-google``.

Pure-Python parsing/writing for the Takeout data types (calendar, contacts,
Keep notes, YouTube-Music shopping list). The standalone tool's FastAPI surface,
SSO identity resolution and module-level env config are intentionally NOT
vendored — in-engine, the acting user and the on-disk data trees are already
known and are passed in explicitly (see ``paths.ImporterPaths``).

This module exposes the shared ``Importer`` protocol and a ``REGISTRY`` of
importer kinds (mirroring the ``action_cards`` registry style). The actual job
wiring — enqueue → dispatch → run — lands in the durable import-job runner
(#864/#868); here we only pin the contract and register the ``google_takeout``
kind as a stub. Submodules that pull heavy optional deps (``icalendar``,
``vobject``, ``ytmusicapi``) are imported lazily so a missing dep never crashes
engine import.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from .paths import ImporterPaths


__all__ = ["ImporterPaths", "ImportPlan", "Importer", "REGISTRY", "register", "get"]


@dataclass(frozen=True)
class ImportPlan:
    """The result of planning an import: what would be written, given selections.

    ``kind`` names the importer; ``writes`` is the concrete per-item work the
    ``run`` step will carry out; ``summary`` is the human-facing preview counts.
    """

    kind: str
    writes: list[dict[str, Any]] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class Importer(Protocol):
    """Contract every import kind implements (wired by #864/#868).

    - ``detect(manifest)`` — inspect an upload/archive manifest and return the
      claims (which data types this kind can import from it).
    - ``plan(archive, selections)`` — turn a chosen subset into an ``ImportPlan``.
    - ``run(plan, progress)`` — execute the plan, emitting progress; returns the
      writes actually performed.
    """

    def detect(self, manifest: Any) -> list[dict[str, Any]]: ...

    def plan(self, archive: Any, selections: Any) -> ImportPlan: ...

    def run(self, plan: ImportPlan, progress: Any) -> list[dict[str, Any]]: ...


REGISTRY: dict[str, Importer] = {}


def register(kind: str, importer: Importer) -> None:
    """Register an importer kind (used by the job runner once kinds are wired)."""
    REGISTRY[kind] = importer


def get(kind: str) -> Importer | None:
    return REGISTRY.get(kind)


class _GoogleTakeoutStub:
    """Placeholder ``google_takeout`` entry — the vendored parsers exist
    (``importers.calendar``/``contacts``/``keep``, ``music_shopping``) but the
    detect/plan/run job wiring lands in #864/#868. Calling it raises so a
    half-wired dispatch fails loudly rather than silently no-op'ing."""

    kind = "google_takeout"

    def detect(self, manifest: Any) -> list[dict[str, Any]]:
        raise NotImplementedError("google_takeout import wiring lands in #864/#868")

    def plan(self, archive: Any, selections: Any) -> ImportPlan:
        raise NotImplementedError("google_takeout import wiring lands in #864/#868")

    def run(self, plan: ImportPlan, progress: Any) -> list[dict[str, Any]]:
        raise NotImplementedError("google_takeout import wiring lands in #864/#868")


register("google_takeout", _GoogleTakeoutStub())

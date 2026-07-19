"""Read-only reader for the existing Obsidian vault (#448, contract §6).

The Obsidian adapter normalizes the household's *hand-written* notes into OKF
concepts. It depends on the `ObsidianReader` Protocol, not the filesystem — so
tests inject a fake and the live path uses `VaultObsidianReader`, a thin
read-only walk of `NOTES_DIR` (the same vault `engine/tools/notes.py` serves).

`VaultObsidianReader` parses each `.md` into a normalized `VaultNote`: its
relative path, a small frontmatter subset (`type`/`title`/`tags`/`timestamp`),
the body with the frontmatter stripped, and its `[[wikilink]]` targets. It is
read-only on the source — only `rglob` + `read_text`, never a write — and the
OKF output goes under a separate `okf/` subtree, so the originals are untouched.

The reader **skips the `okf/` subtree** so the adapter never re-ingests its own
output, and skips `facts/` (the dynamic-skills capture dir, already structured).
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


_MAX_BYTES = 256 * 1024  # skip pathological files; notes are small markdown
_WIKILINK = re.compile(r"\[\[([^\]|]+?)(?:\|[^\]]*)?\]\]")
_FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n?", re.DOTALL)


@dataclass(frozen=True)
class VaultNote:
    """The normalized subset of one hand-written vault note the adapter maps.

    `relpath` is relative to the vault root and is the stable `external_id`
    (`obsidian:<relpath>`). `folder` is the note's top-level directory (used as a
    type hint when frontmatter carries none). `wikilinks` are the raw
    `[[target]]` targets in the body, in document order.
    """

    relpath: str
    folder: str
    title: str
    body: str
    note_type: str = ""
    timestamp: str = ""
    tags: list[str] = field(default_factory=list)
    wikilinks: list[str] = field(default_factory=list)
    # The full flat scalar frontmatter map, so an adapter can read a note-type's
    # own keys (a physical-media note's artist/album/medium, #880) without adding
    # a VaultNote field per type.
    frontmatter: dict[str, str] = field(default_factory=dict)


class ObsidianReader(Protocol):
    """Read-only access to the existing vault. Injectable for tests."""

    def iter_notes(self) -> Iterator[VaultNote]:
        """Yield every hand-written note in the vault (the `okf/` subtree and
        the `facts/` capture dir excluded)."""
        ...


class VaultObsidianReader:
    """Walk `NOTES_DIR` read-only and yield parsed `VaultNote`s."""

    # Vault subtrees that are not hand-written knowledge: our own OKF output and
    # the dynamic-skills fact-capture dir (already structured, ingested elsewhere).
    _SKIP_DIRS = ("okf", "facts")

    def __init__(self, notes_dir: str):
        self._root = Path(notes_dir)

    def iter_notes(self) -> Iterator[VaultNote]:
        if not self._root.is_dir():
            return
        for path in sorted(self._root.rglob("*.md")):
            relpath = path.relative_to(self._root).as_posix()
            if self._is_machine_subtree(relpath):
                continue
            if not path.is_file():
                continue
            try:
                if path.stat().st_size > _MAX_BYTES:
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            yield self._parse(relpath, text)

    def _is_machine_subtree(self, relpath: str) -> bool:
        """Our own OKF output / fact-capture dir — at the vault root and under a
        per-user path (`users/<uid>/okf|facts/...`, #576). A hand-written note
        directly under `users/<uid>/` is still ingested (path-scoped private)."""
        parts = relpath.split("/")
        if len(parts) > 1 and parts[0] in self._SKIP_DIRS:
            return True
        if len(parts) > 3 and parts[0] == "users" and parts[2] in self._SKIP_DIRS:
            return True
        return False

    def _parse(self, relpath: str, text: str) -> VaultNote:
        front, body = _split_frontmatter(text)
        folder = relpath.split("/", 1)[0] if "/" in relpath else ""
        return VaultNote(
            relpath=relpath,
            folder=folder,
            title=front.get("title", "") or _heading(body) or Path(relpath).stem,
            body=body,
            note_type=front.get("type", ""),
            timestamp=front.get("timestamp", ""),
            tags=_list_value(front.get("tags", "")),
            wikilinks=list(dict.fromkeys(_WIKILINK.findall(body))),
            frontmatter=front,
        )


def _split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Return (scalar frontmatter map, body). Only the flat `key: value` lines
    the adapter needs are parsed; lists are kept as their raw inline form."""
    m = _FRONTMATTER.match(text)
    if not m:
        return {}, text.strip("\n")
    front: dict[str, str] = {}
    for line in m.group(1).splitlines():
        key, sep, value = line.partition(":")
        if sep and not key.startswith((" ", "\t", "-")):
            front[key.strip()] = value.strip().strip("'\"")
    return front, text[m.end() :].strip("\n")


def _list_value(raw: str) -> list[str]:
    """Parse an inline frontmatter list (`[a, b]` or `a, b`) into items."""
    raw = raw.strip().strip("[]")
    return [item.strip().strip("'\"") for item in raw.split(",") if item.strip()]


def _heading(body: str) -> str:
    m = re.search(r"(?m)^#\s+(.+?)\s*$", body)
    return m.group(1).strip() if m else ""

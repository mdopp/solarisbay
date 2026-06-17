"""Read the Obsidian notes vault for topic-filtered retrieval.

The notes vault (`/opt/data/notes`, Syncthing-synced) is the household
knowledge base — the `notes-search` skill greps it on-demand, the ingestion
skills write into it. Once a chat carries a primary topic, ingestion stamps
`#topic/<slug>` into the note's frontmatter `tags` (#243). This module reads
those tags back so the topic dashboard can list everything for a topic (#244).

The topic tag appears in two written forms, both of which we match:
  - a frontmatter `tags` list entry `topic/<slug>` (media-ingestion,
    daily-chronicle write it without the `#` in YAML list form), and
  - an inline `#topic/<slug>` token (dynamic-skills fact blocks).
Slugs may be hierarchical (`projekt/wintergarten`), so the match is on the
exact slug, separated by a `/` boundary — `topic/projekt/wintergarten` matches
slug `projekt/wintergarten`, not `projekt`.

Per-resident isolation (D3): a resident sees their own topic's notes. A note
records its writer in frontmatter `added_by: <uid>`; we filter to the caller's
uid. Notes with no `added_by` (system/legacy) are treated as shared and shown.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

_MAX_BYTES = 256 * 1024  # skip pathological files; notes are small markdown


def _topic_pattern(slug: str) -> re.Pattern[str]:
    # Match SLUG's topic tag in either written form: the inline `#topic/<slug>`
    # token and the bare `topic/<slug>` frontmatter-list entry (`#?`). The
    # boundaries stop `projekt/wintergarten` from also matching a longer
    # `projekt/wintergartendach` slug or a `.../wintergarten/sub` child tag.
    return re.compile(rf"(?<![\w/])#?topic/{re.escape(slug)}(?![\w/-])")


def _added_by(text: str) -> str | None:
    """The note's `added_by:` frontmatter uid, or None when absent."""
    m = re.search(r"(?mi)^added_by:\s*(.+?)\s*$", text)
    if not m:
        return None
    value = m.group(1).strip().strip("'\"")
    return value or None


def notes_for_topic(
    notes_dir: str | Path, slug: str, owner_uid: str
) -> list[dict[str, Any]]:
    """Notes tagged `#topic/<slug>` the resident may see, newest-path first.

    Walks `notes_dir` for `.md` files carrying the topic tag (either written
    form), keeps those owned by `owner_uid` (or unowned/shared), and returns
    `[{path, title}]` — `path` relative to the vault root (for display/citation),
    `title` the first `# ` heading or the filename stem. Empty when the vault is
    missing, the slug is blank, or nothing matches.
    """
    root = Path(notes_dir)
    if not slug or not root.is_dir():
        return []
    pattern = _topic_pattern(slug)
    out: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*.md")):
        if not path.is_file():
            continue
        try:
            if path.stat().st_size > _MAX_BYTES:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if not pattern.search(text):
            continue
        added_by = _added_by(text)
        if added_by is not None and added_by != owner_uid:
            continue
        out.append(
            {
                "path": str(path.relative_to(root)),
                "title": _title(text, path.stem),
            }
        )
    return out


def notes_mentioning(
    notes_dir: str | Path, names: list[str], owner_uid: str, limit: int = 20
) -> list[dict[str, Any]]:
    """Notes whose text mentions any of `names` — the concept page's source/
    backlink docs (#502). Case-insensitive whole-word match on a name, excluding
    the OKF subtree (those are the canonical concept files, surfaced separately).
    Per-resident: a note's `added_by` must match (or be absent/shared). Returns
    `[{path, title}]` relative to the vault, capped. Empty when nothing matches.
    """
    root = Path(notes_dir)
    wanted = [n for n in dict.fromkeys(names) if n]
    if not wanted or not root.is_dir():
        return []
    patterns = [
        re.compile(rf"(?<!\w){re.escape(n)}(?!\w)", re.IGNORECASE) for n in wanted
    ]
    out: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*.md")):
        if not path.is_file() or "okf" in path.relative_to(root).parts[:1]:
            continue
        try:
            if path.stat().st_size > _MAX_BYTES:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if not any(p.search(text) for p in patterns):
            continue
        added_by = _added_by(text)
        if added_by is not None and added_by != owner_uid:
            continue
        out.append(
            {"path": str(path.relative_to(root)), "title": _title(text, path.stem)}
        )
        if len(out) >= limit:
            break
    return out


def _title(text: str, fallback: str) -> str:
    """The note's first `# ` heading, or the filename stem as a fallback."""
    m = re.search(r"(?m)^#\s+(.+?)\s*$", text)
    return m.group(1).strip() if m else fallback

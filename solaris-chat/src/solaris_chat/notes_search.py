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

Per-resident isolation (D3 / #576): a resident sees their own notes plus shared
household notes — never another resident's private note. Ownership is
**path-based** (the operator's model): a note under `users/<uid>/` is private to
that uid; the rest of the vault is shared. When the path doesn't scope it,
ownership falls back to the frontmatter — `added_by: <uid>` (model/hand-written
notes), then `resident: <uid>` (OKF concept files). Notes with neither signal
are treated as shared and shown. Default-deny: an unknown/unauthenticated caller
(`household`) sees only shared notes, never a personal one.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any

_MAX_BYTES = 256 * 1024  # skip pathological files; notes are small markdown

# The shared-data sentinel uid (mirrors EngineProfile.default_uid). A note owned
# by this uid — or by no one — is visible to every resident.
SHARED_UID = "household"

# A note under `users/<uid>/...` is private to `<uid>`, whatever its frontmatter.
_USER_PATH_RE = re.compile(r"^users/([^/]+)/")

# An upload companion: the raw extraction scratch beside an uploaded file, at the
# shared `uploads/<file>.md` or per-user `users/<uid>/uploads/<file>.md`. The
# obsidian ingest projects its text into a derived OKF note; the companion itself
# is not a note (#998).
_UPLOAD_COMPANION_RE = re.compile(r"(?:^|/)uploads/[^/]+\.md$")

# The vault is a Syncthing folder: it carries a `.stversions/` tree with tens of
# thousands of historical file copies, plus `.stfolder`, and other tool droppings
# (`.git`, `.obsidian`, `.trash`). None are notes; recursing into them made an
# `rglob("*.md")` walk of the real vault never finish (#705). `processed/` holds
# already-consolidated inbox exports, also not browsable notes. `uploads/` holds
# the raw upload companions — extraction scratch whose text the obsidian ingest
# projects into a derived OKF note; the companion itself is NOT a note, so keeping
# it out of the note walk stops it colliding with its own OKF note on `.note`
# search (#998). Prune these whole subtrees at the directory boundary — an `rglob`
# cannot prune, `os.walk` can.
_PRUNE_DIRS = frozenset({"processed", "exports", "uploads"})


# Cap the vault walk so a pathological (Syncthing-runaway) vault can never wedge
# the caller: the walk degrades to a partial result once it hits this many `.md`
# files. Far above any plausible household note count.
_VAULT_WALK_BUDGET = 20000


def iter_vault_md(
    root: Path, budget: int | None = _VAULT_WALK_BUDGET
) -> Iterator[Path]:
    """Yield `.md` file paths under `root`, pruning non-note subtrees (#705).

    Skips any dot-directory (`.stversions`, `.stfolder`, `.git`, `.obsidian`,
    `.trash`, …) and the `_PRUNE_DIRS` (already-consolidated inbox trees), so a
    Syncthing vault's history copies never inflate the walk. Bounded by `budget`
    so a pathological vault can never wedge the caller — the walk degrades to
    partial rather than hanging; `budget=None` is unbounded (the FTS backfill's
    full-vault pass, #830). Directory order is sorted for stable output; callers
    that need a global sort still sort the yielded paths.
    """
    if not root.is_dir():
        return
    seen = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            d for d in dirnames if not d.startswith(".") and d not in _PRUNE_DIRS
        )
        for name in sorted(filenames):
            if not name.endswith(".md"):
                continue
            if budget is not None and seen >= budget:
                return
            seen += 1
            yield Path(dirpath) / name


# A journal entry's date, wherever the daily-chronicle model dropped it (#709):
# `journal/<date>.md`, `journal/journal_<date>.md`, or `journal/<YYYY>/<date>.md`.
# The path is prompt-driven, so the small model varies the convention; a match
# here recovers the `<YYYY-MM-DD>` the entry is FOR so every variant collapses to
# one canonical file.
_JOURNAL_DATE_RE = re.compile(
    r"(?:^|/)journal/(?:[^/]*/)?(?:journal_)?(\d{4}-\d{2}-\d{2})(?:[^/]*)?\.md$"
)


def journal_date(relpath: str) -> str | None:
    """The `YYYY-MM-DD` a vault path is a journal entry for, else None (#709).

    Matches any of the three seen conventions (`journal/<date>.md`,
    `journal/journal_<date>.md`, `journal/<YYYY>/<date>.md`) so a same-day
    duplicate under any of them resolves to the same date."""
    m = _JOURNAL_DATE_RE.search(relpath.replace("\\", "/"))
    return m.group(1) if m else None


def canonical_journal_path(relpath: str) -> str | None:
    """The one canonical path a journal entry must live at, else None (#709).

    `journal/<YYYY>/<YYYY-MM-DD>.md` — deterministic from the date, so a nightly
    re-run overwrites in place instead of spawning a new variant."""
    date = journal_date(relpath)
    if date is None:
        return None
    return f"journal/{date[:4]}/{date}.md"


def is_upload_companion(relpath: str) -> bool:
    """True when a vault-relative path is an upload companion (#998).

    The companion is extraction scratch, not a note: excluded from the note walk
    and swept from the FTS index so it can't collide with its derived OKF note."""
    return _UPLOAD_COMPANION_RE.search(relpath.replace("\\", "/")) is not None


def resident_for_path(relpath: str) -> str | None:
    """The uid a vault-relative path scopes to, or None when it's shared.

    A note under `users/<uid>/...` belongs to `<uid>`; everything else is
    shared. Path ownership only — no frontmatter (so the structured-ingest side
    can scope a concept by its source path the same way reads do)."""
    m = _USER_PATH_RE.match(relpath.replace("\\", "/"))
    return m.group(1) if m else None


def owner_of(relpath: str, text: str) -> str | None:
    """The note's owner uid, or None when unowned (shared).

    Path wins: a note under `users/<uid>/` is owned by `<uid>` regardless of
    frontmatter. Otherwise fall back to the frontmatter — `added_by:` first
    (model/hand-written notes), then `resident:` (OKF concept files; reading it
    closes the leak where a `resident: <uid>` OKF file surfaced via search)."""
    path_owner = resident_for_path(relpath)
    if path_owner is not None:
        return path_owner
    return _added_by(text) or _resident(text)


def is_visible(added_by: str | None, caller_uid: str) -> bool:
    """Whether a note owned by `added_by` may surface for `caller_uid` (#576).

    Access model: a resident sees their own (`added_by == caller_uid`) plus the
    shared pool (`added_by` is the household sentinel or unset). An unknown
    caller is `household`, so it sees only the shared pool — never a personal
    note (default-deny against a cross-user leak)."""
    return added_by in (None, SHARED_UID, caller_uid)


def _topic_pattern(slug: str) -> re.Pattern[str]:
    # Match SLUG's topic tag in either written form: the inline `#topic/<slug>`
    # token and the bare `topic/<slug>` frontmatter-list entry (`#?`). The
    # boundaries stop `projekt/wintergarten` from also matching a longer
    # `projekt/wintergartendach` slug or a `.../wintergarten/sub` child tag.
    return re.compile(rf"(?<![\w/])#?topic/{re.escape(slug)}(?![\w/-])")


def _added_by(text: str) -> str | None:
    """The note's `added_by:` frontmatter uid, or None when absent."""
    return _frontmatter_value(text, "added_by")


def _resident(text: str) -> str | None:
    """The note's `resident:` frontmatter uid (OKF files), or None when absent.

    `household` is the shared sentinel, not an owner — treat it as unowned so a
    shared OKF concept stays visible to everyone."""
    value = _frontmatter_value(text, "resident")
    return None if value == SHARED_UID else value


def _frontmatter_value(text: str, key: str) -> str | None:
    m = re.search(rf"(?mi)^{key}:\s*(.+?)\s*$", text)
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
    for path in sorted(iter_vault_md(root)):
        try:
            if path.stat().st_size > _MAX_BYTES:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if not pattern.search(text):
            continue
        rel = str(path.relative_to(root))
        if not is_visible(owner_of(rel, text), owner_uid):
            continue
        out.append({"path": rel, "title": _title(text, path.stem)})
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
    for path in sorted(iter_vault_md(root)):
        if "okf" in path.relative_to(root).parts[:1]:
            continue
        try:
            if path.stat().st_size > _MAX_BYTES:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if not any(p.search(text) for p in patterns):
            continue
        rel = str(path.relative_to(root))
        if not is_visible(owner_of(rel, text), owner_uid):
            continue
        out.append({"path": rel, "title": _title(text, path.stem)})
        if len(out) >= limit:
            break
    return out


def notes_wikilinking(
    notes_dir: str | Path,
    names: list[str],
    okf_path: str | None,
    owner_uid: str,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Vault notes whose `[[ ]]` link targets the concept — the other half of
    the entity page's backlinks (#505), alongside chat-turn mentions.

    A target matches when a `[[ ]]` link names the concept (a `name`/alias) or
    points at its OKF concept file — by full path (`okf/people/anna`), the
    `okf/`-stripped path (`people/anna`), or the bare stem (`anna`); a trailing
    `.md` and an optional `|label` are ignored. The okf/ subtree is skipped (a
    concept file's own Relationships aren't a backlink to it). Per-resident on
    `added_by`. Returns `[{path, title}]` relative to the vault, capped.
    """
    root = Path(notes_dir)
    wanted = {n.casefold() for n in names if n}
    if okf_path:
        stem = okf_path[len("okf/") :] if okf_path.startswith("okf/") else okf_path
        if stem.endswith(".md"):
            stem = stem[:-3]
        wanted.update(
            {okf_path.casefold(), stem.casefold(), stem.rsplit("/", 1)[-1].casefold()}
        )
    if not wanted or not root.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for path in sorted(iter_vault_md(root)):
        if "okf" in path.relative_to(root).parts[:1]:
            continue
        try:
            if path.stat().st_size > _MAX_BYTES:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if not any(
            _wikilink_target(m).casefold() in wanted for m in _WIKILINK_RE.findall(text)
        ):
            continue
        rel = str(path.relative_to(root))
        if not is_visible(owner_of(rel, text), owner_uid):
            continue
        out.append({"path": rel, "title": _title(text, path.stem)})
        if len(out) >= limit:
            break
    return out


_WIKILINK_RE = re.compile(r"\[\[([^\]]+?)\]\]")


def _wikilink_target(inner: str) -> str:
    """The link target from a `[[ ]]` body — the part before `|`, sans `.md`."""
    target = inner.split("|", 1)[0].strip()
    return target[:-3] if target.endswith(".md") else target


def _title(text: str, fallback: str) -> str:
    """The note's first `# ` heading, or the filename stem as a fallback."""
    m = re.search(r"(?m)^#\s+(.+?)\s*$", text)
    return m.group(1).strip() if m else fallback

"""Read the Solaris skill pack off the filesystem.

Hermes' `/v1/skills` lists name + description only; `/v1/skills/{name}`
404s — there is no body API — so the panel reads the markdown straight off
the bind-mounted pack (`SKILLS_DIR`, the host `solarisbay/skills` dir mounted
read-only for reads). This is the shipped *standard set*: everyone reads,
only admins edit. Each skill is `<dir>/<name>/SKILL.md` with a YAML
frontmatter block (name/description/version) and a markdown body.

Admin edits write the raw SKILL.md back through a read-write mount of the
same pack (the skills dir is host-owned, so the chat pod — rootless,
container-root → the host user — can replace files there). Hermes reads a
skill *body* live; a frontmatter change (name/description) needs a Hermes
restart to re-register, which the caller surfaces.

A skill id is its directory name (filesystem-safe, stable). We never accept
a path separator in an id, so a request can't escape the pack.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from typing import Any


def _split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Split a `---`-delimited YAML frontmatter head from the markdown body.

    A deliberately small parser (no PyYAML dep): the pack's frontmatter is
    flat `key: value` scalars. Unknown/complex lines are ignored; the body
    is everything after the closing `---`.
    """
    if not text.startswith("---"):
        return {}, text
    lines = text.splitlines()
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        return {}, text
    meta: dict[str, str] = {}
    for line in lines[1:end]:
        key, sep, value = line.partition(":")
        if not sep:
            continue
        meta[key.strip()] = value.strip().strip("'\"")
    body = "\n".join(lines[end + 1 :]).lstrip("\n")
    return meta, body


def _is_valid_id(skill_id: str) -> bool:
    return bool(skill_id) and "/" not in skill_id and skill_id not in (".", "..")


# A definition's `kind` (#480): only `skill` belongs in the model-selectable
# pool; the other three fire deterministically (user `/`, the clock, an event).
KINDS = ("skill", "command", "hook", "scheduler")
# A definition without an explicit `kind:` frontmatter is a skill — the legacy
# default before the taxonomy split, so the existing pack stays valid.
_DEFAULT_KIND = "skill"
_DEFAULT_SCOPE = "household"


def def_kind(meta: dict[str, str]) -> str:
    kind = meta.get("kind", "").strip().lower()
    return kind if kind in KINDS else _DEFAULT_KIND


def _iter_defs(skills_dir: str | Path):
    """Yield `(id, meta, body, file)` for every `<id>/SKILL.md` in the pack."""
    root = Path(skills_dir)
    if not root.is_dir():
        return
    for child in root.iterdir():
        skill_file = child / "SKILL.md"
        if not child.is_dir() or not skill_file.is_file():
            continue
        meta, body = _split_frontmatter(skill_file.read_text(encoding="utf-8"))
        yield child.name, meta, body, skill_file


def list_defs(skills_dir: str | Path, kind: str) -> list[dict[str, str]]:
    """List one kind's registry: `[{id, name, description, kind, scope}]`,
    sorted by name. The per-kind walk that replaces the single flat list —
    `kind` is read from each definition's frontmatter (default `skill`)."""
    out: list[dict[str, str]] = []
    for def_id, meta, _body, _file in _iter_defs(skills_dir):
        if def_kind(meta) != kind:
            continue
        out.append(
            {
                "id": def_id,
                "name": meta.get("name") or def_id,
                "description": meta.get("description", ""),
                "kind": kind,
                "scope": meta.get("scope") or _DEFAULT_SCOPE,
                # The inline `/`-trigger and its prompt-line hint for a typeable
                # command (set on command-kind defs and the dual status/notes/
                # audit skills); empty for the rest. Drives the `/commands` card
                # and the slash-pool entry.
                "command": meta.get("command", ""),
                "argument-hint": meta.get("argument-hint", ""),
            }
        )
    out.sort(key=lambda s: s["name"].lower())
    return out


def hooks_for_event(skills_dir: str | Path, event: str) -> list[str]:
    """The hook-kind definition ids bound to `event` (their frontmatter
    `event:` field), sorted by id — the registry server flow points resolve a
    hook by, instead of hardcoding a skill id. Phase 5 (#483) wires the actual
    flow points to it; foundation just exposes the lookup."""
    out = [
        def_id
        for def_id, meta, _body, _file in _iter_defs(skills_dir)
        if def_kind(meta) == "hook" and meta.get("event", "").strip() == event
    ]
    out.sort()
    return out


def list_skills(skills_dir: str | Path) -> list[dict[str, str]]:
    """List the skill-kind pack: `[{id, name, description}]`, sorted by name.

    A directory counts as a skill when it holds a `SKILL.md` and its
    frontmatter `kind` is `skill` (or absent — the legacy default). Missing
    dir = empty list (the mount may not be present in offline test).
    """
    return [
        {"id": d["id"], "name": d["name"], "description": d["description"]}
        for d in list_defs(skills_dir, "skill")
    ]


def read_skill(skills_dir: str | Path, skill_id: str) -> dict[str, Any] | None:
    """Return `{id, name, description, kind, scope, body, raw}` for one
    definition, or None.

    `body` is the markdown after the frontmatter — what the panel renders.
    `raw` is the full SKILL.md (frontmatter + body) — what the editor loads.
    """
    if not _is_valid_id(skill_id):
        return None
    skill_file = Path(skills_dir) / skill_id / "SKILL.md"
    if not skill_file.is_file():
        return None
    raw = skill_file.read_text(encoding="utf-8")
    meta, body = _split_frontmatter(raw)
    return {
        "id": skill_id,
        "name": meta.get("name") or skill_id,
        "description": meta.get("description", ""),
        "kind": def_kind(meta),
        "scope": meta.get("scope") or _DEFAULT_SCOPE,
        "body": body,
        "raw": raw,
    }


def read_def(skills_dir: str | Path, kind: str, def_id: str) -> dict[str, Any] | None:
    """Read one definition, but only when its frontmatter `kind` matches the
    requested registry — so `/api/defs/scheduler/status` 404s rather than
    leaking a skill through the wrong card."""
    one = read_skill(skills_dir, def_id)
    if one is None or one["kind"] != kind:
        return None
    return one


def delete_def(skills_dir: str | Path, kind: str, def_id: str) -> bool:
    """Delete a definition's directory (the whole `<id>/`), but only when its
    frontmatter `kind` matches. Returns True on delete, False when the id is
    invalid / not found / wrong kind."""
    if read_def(skills_dir, kind, def_id) is None:
        return False
    shutil.rmtree(Path(skills_dir) / def_id)
    return True


def write_skill(
    skills_dir: str | Path, skill_id: str, content: str
) -> dict[str, Any] | None:
    """Replace an existing skill's SKILL.md with `content` (the full raw
    markdown). Returns `{id, frontmatter_changed}` or None when the id is
    invalid or the skill doesn't exist (we only edit the shipped set, never
    create arbitrary files).

    `frontmatter_changed` is True when name/description/version differs from
    the old file — the signal that Hermes needs a restart to re-register the
    skill (a body-only edit is picked up live). The write is atomic (temp in
    the same dir + os.replace) so a reader never sees a half-written file.
    """
    if not _is_valid_id(skill_id):
        return None
    skill_file = Path(skills_dir) / skill_id / "SKILL.md"
    if not skill_file.is_file():
        return None
    old_meta, _ = _split_frontmatter(skill_file.read_text(encoding="utf-8"))
    new_meta, _ = _split_frontmatter(content)
    _atomic_write(skill_file, content)
    return {"id": skill_id, "frontmatter_changed": new_meta != old_meta}


def _atomic_write(skill_file: Path, content: str) -> None:
    skill_file.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=str(skill_file.parent), prefix=".SKILL.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp, skill_file)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_def(
    skills_dir: str | Path, kind: str, def_id: str, content: str
) -> dict[str, Any] | None:
    """Create-or-update one definition in the `kind` registry (the CRUD PUT).

    Returns `{id, created, frontmatter_changed}`, or None when the id is
    invalid or the new content's frontmatter `kind` contradicts the registry
    being written (so a PUT to /api/defs/skill can't drop a scheduler in). A
    create is allowed (the editor cards add new definitions); an update to an
    existing def of a *different* kind is rejected. Atomic write.
    """
    if not _is_valid_id(def_id):
        return None
    new_meta, _ = _split_frontmatter(content)
    if def_kind(new_meta) != kind:
        return None
    skill_file = Path(skills_dir) / def_id / "SKILL.md"
    existing = read_skill(skills_dir, def_id)
    if existing is not None and existing["kind"] != kind:
        return None
    old_meta, _ = (
        _split_frontmatter(skill_file.read_text(encoding="utf-8"))
        if skill_file.is_file()
        else ({}, "")
    )
    _atomic_write(skill_file, content)
    return {
        "id": def_id,
        "created": existing is None,
        "frontmatter_changed": new_meta != old_meta,
    }

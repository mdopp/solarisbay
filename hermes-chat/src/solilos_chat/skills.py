"""Read the Solilos skill pack off the filesystem.

Hermes' `/v1/skills` lists name + description only; `/v1/skills/{name}`
404s — there is no body API — so the panel reads the markdown straight off
the bind-mounted pack (`SKILLS_DIR`, the host `solbay/skills` dir mounted
read-only). This is the shipped *standard set*: everyone reads, only admins
edit (edit lands in phase 2). Each skill is `<dir>/<name>/SKILL.md` with a
YAML frontmatter block (name/description/version) and a markdown body.

A skill id is its directory name (filesystem-safe, stable). We never accept
a path separator in an id, so a request can't escape the pack.
"""

from __future__ import annotations

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


def list_skills(skills_dir: str | Path) -> list[dict[str, str]]:
    """List the pack: `[{id, name, description}]`, sorted by name.

    A directory counts as a skill when it holds a `SKILL.md`. Missing dir =
    empty list (the mount may not be present in offline test).
    """
    root = Path(skills_dir)
    if not root.is_dir():
        return []
    out: list[dict[str, str]] = []
    for child in root.iterdir():
        skill_file = child / "SKILL.md"
        if not child.is_dir() or not skill_file.is_file():
            continue
        meta, _ = _split_frontmatter(skill_file.read_text(encoding="utf-8"))
        out.append(
            {
                "id": child.name,
                "name": meta.get("name") or child.name,
                "description": meta.get("description", ""),
            }
        )
    out.sort(key=lambda s: s["name"].lower())
    return out


def read_skill(skills_dir: str | Path, skill_id: str) -> dict[str, Any] | None:
    """Return `{id, name, description, body}` for one skill, or None.

    `body` is the markdown after the frontmatter — what the panel renders.
    """
    if not _is_valid_id(skill_id):
        return None
    skill_file = Path(skills_dir) / skill_id / "SKILL.md"
    if not skill_file.is_file():
        return None
    meta, body = _split_frontmatter(skill_file.read_text(encoding="utf-8"))
    return {
        "id": skill_id,
        "name": meta.get("name") or skill_id,
        "description": meta.get("description", ""),
        "body": body,
    }

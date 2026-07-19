"""Import Google Keep notes into the acting user's Obsidian vault.

Google Takeout exports Keep as one ``.json`` per note (``Takeout/Keep/*.json``)
plus attachment files (images/audio) referenced by ``filePath``. We convert each
note to an Obsidian Markdown file with YAML frontmatter and write it under the
injected ``target`` dir; attachments are copied into an ``attachments/``
subfolder and embedded with Obsidian ``![[...]]`` links.

Accepts either individual ``.json`` (+ attachment) uploads or a single ``.zip``
of the Keep folder (attachments only resolve when they're in the upload set,
i.e. use the zip to keep them).
"""

from __future__ import annotations

import io
import json
import re
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from .. import ImportPlan

_NS = uuid.UUID("6f1a1c2e-9b1e-4b7a-9c2d-000000000003")
_FS_UNSAFE = re.compile(r'[/\\:*?"<>|\x00-\x1f]')
_JSON_META = {"Labels.txt"}  # non-note files to ignore


# ---------------------------------------------------------------------------
# Upload handling
# ---------------------------------------------------------------------------


def expand_uploads(files: list[tuple[str, bytes]]) -> dict[str, bytes]:
    """Flatten uploads into a basename->bytes map, expanding any ``.zip``."""
    out: dict[str, bytes] = {}
    for name, data in files:
        if name.lower().endswith(".zip"):
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                for info in zf.infolist():
                    if info.is_dir():
                        continue
                    out[_basename(info.filename)] = zf.read(info)
        else:
            out[_basename(name)] = data
    return out


def _basename(path: str) -> str:
    return path.replace("\\", "/").rsplit("/", 1)[-1]


def _is_note(name: str, data: bytes) -> bool:
    if not name.lower().endswith(".json") or name in _JSON_META:
        return False
    try:
        obj = json.loads(data)
    except (ValueError, UnicodeDecodeError):
        return False
    return isinstance(obj, dict) and (
        "textContent" in obj or "listContent" in obj or "createdTimestampUsec" in obj
    )


def iter_notes(file_map: dict[str, bytes]):
    for name, data in file_map.items():
        if _is_note(name, data):
            yield name, json.loads(data)


# ---------------------------------------------------------------------------
# Conversion
# ---------------------------------------------------------------------------


def _iso(usec) -> str | None:
    if not usec:
        return None
    return datetime.fromtimestamp(int(usec) / 1_000_000, tz=timezone.utc).isoformat()


def _safe_filename(title: str, note: dict) -> str:
    base = _FS_UNSAFE.sub("-", (title or "").strip()).strip(" .-")
    short = uuid.uuid5(_NS, f"{title}|{note.get('createdTimestampUsec', '')}").hex[:8]
    base = base or "Note"
    return f"{base[:80]} {short}.md"


def _yaml_list(values) -> str:
    return "[" + ", ".join(json.dumps(v, ensure_ascii=False) for v in values) + "]"


def note_to_markdown(note: dict) -> tuple[str, list[str]]:
    """Return (markdown_text, [attachment basenames referenced])."""
    title = note.get("title", "").strip()
    created = _iso(note.get("createdTimestampUsec"))
    updated = _iso(note.get("userEditedTimestampUsec"))
    labels = [lbl.get("name", "") for lbl in note.get("labels", []) if lbl.get("name")]

    fm = ["---", "source: google-keep"]
    if title:
        fm.append(f"title: {json.dumps(title, ensure_ascii=False)}")
    if created:
        fm.append(f"created: {created}")
    if updated:
        fm.append(f"updated: {updated}")
    if labels:
        fm.append(f"tags: {_yaml_list(labels)}")
    if note.get("isPinned"):
        fm.append("pinned: true")
    if note.get("isArchived"):
        fm.append("archived: true")
    color = note.get("color")
    if color and color != "DEFAULT":
        fm.append(f"color: {color.lower()}")
    fm.append("---")

    body: list[str] = []
    if title:
        body.append(f"# {title}\n")

    if "listContent" in note:
        for item in note["listContent"]:
            mark = "x" if item.get("isChecked") else " "
            body.append(f"- [{mark}] {item.get('text', '')}")
        body.append("")
    elif note.get("textContent"):
        body.append(note["textContent"])
        body.append("")

    attachments: list[str] = []
    for att in note.get("attachments", []):
        fp = att.get("filePath") or att.get("file_path")
        if not fp:
            continue
        bn = _basename(fp)
        attachments.append(bn)
        body.append(f"![[attachments/{bn}]]")
    if attachments:
        body.append("")

    links = [a for a in note.get("annotations", []) if a.get("url")]
    if links:
        body.append("## Links")
        for a in links:
            label = a.get("title") or a.get("url")
            body.append(f"- [{label}]({a['url']})")
        body.append("")

    return "\n".join(fm) + "\n\n" + "\n".join(body).rstrip() + "\n", attachments


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def preview(files: list[tuple[str, bytes]]) -> dict:
    file_map = expand_uploads(files)
    notes = list(iter_notes(file_map))
    kept = [n for _, n in notes if not n.get("isTrashed")]
    trashed = sum(1 for _, n in notes if n.get("isTrashed"))
    attachments = sum(len(n.get("attachments", [])) for n in kept)
    samples = [n.get("title") or "(ohne Titel)" for n in kept[:5]]
    return {
        "type": "keep",
        "notes": len(kept),
        "trashed_skipped": trashed,
        "attachments": attachments,
        "samples": samples,
    }


def do_import(target: Path, files: list[tuple[str, bytes]]) -> dict:
    file_map = expand_uploads(files)
    attach_dir = target / "attachments"
    target.mkdir(parents=True, exist_ok=True)

    written = 0
    attachments_copied = 0
    missing_attachments: list[str] = []

    for _, note in iter_notes(file_map):
        if note.get("isTrashed"):
            continue
        md, atts = note_to_markdown(note)
        fname = _safe_filename(note.get("title", ""), note)
        (target / fname).write_text(md, encoding="utf-8")
        written += 1
        for bn in atts:
            if bn in file_map:
                attach_dir.mkdir(parents=True, exist_ok=True)
                (attach_dir / bn).write_bytes(file_map[bn])
                attachments_copied += 1
            else:
                missing_attachments.append(bn)

    return {
        "type": "keep",
        "written": written,
        "attachments_copied": attachments_copied,
        "attachments_missing": len(missing_attachments),
        "target": str(target),
    }


class KeepImporter:
    """Registrable ``keep`` importer kind.

    Converts each Takeout Keep note to Obsidian Markdown (``plan``) and writes
    the ``.md`` (+ copied attachments) into the owner's vault subtree
    (``run``) — the injected ``target`` ``Path``, not a DAV collection. The
    written notes are projected to OKF concepts on the next nightly
    ``ObsidianIngest`` run; no new ingest code is added.
    """

    kind = "keep"

    def detect(self, manifest) -> list[dict]:
        return [{"kind": self.kind, "type": "keep"}]

    def plan(self, archive, selections) -> ImportPlan:
        files: list[tuple[str, bytes]] = archive["files"]
        return ImportPlan(
            kind=self.kind,
            writes=[{"files": files}],
            summary=preview(files),
        )

    def run(self, plan: ImportPlan, progress) -> list[dict]:
        target: Path = progress["target"]
        return [do_import(target, write["files"]) for write in plan.writes]

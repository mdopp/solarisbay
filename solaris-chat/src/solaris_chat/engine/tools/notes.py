"""Notes-vault tools: search, read, and durable fact capture.

The Obsidian vault (`/opt/data/notes`, Syncthing-synced) is the household
knowledge base. `notes_search` greps it, `notes_read` returns one note,
`fact_store` appends a dated fact file (the dynamic-skills policy: facts,
preferences, household routines — never device state). This is also the
engine's retrieval seam: future Immich/CalDAV/chat retrievers register here
as further tools without touching the loop.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from solaris_chat import notes_search
from solaris_chat.engine.tools import Tool

_MAX_BYTES = 256 * 1024
_MAX_HITS = 8


def build_notes_tools(notes_dir: str, uid_getter) -> list[Tool]:
    root = Path(notes_dir)

    async def search(args: dict[str, Any]) -> str:
        query = str(args.get("query") or "").strip()
        if not query or not root.is_dir():
            return "[]"
        # Default-deny per-owner scope (#576): only the caller's own notes plus
        # the shared household pool — never another resident's private note. An
        # unknown caller resolves to `household`, so it sees the shared pool only.
        caller_uid = uid_getter()
        terms = [t.lower() for t in query.split() if t]
        hits = []
        for path in sorted(root.rglob("*.md")):
            try:
                if not path.is_file() or path.stat().st_size > _MAX_BYTES:
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if not notes_search.is_visible(notes_search.owner_of(text), caller_uid):
                continue
            lower = text.lower()
            if not all(t in lower for t in terms):
                continue
            idx = lower.find(terms[0])
            snippet = text[max(0, idx - 80) : idx + 160].replace("\n", " ")
            hits.append({"path": str(path.relative_to(root)), "snippet": snippet})
            if len(hits) >= _MAX_HITS:
                break
        return json.dumps(hits, ensure_ascii=False)

    async def read(args: dict[str, Any]) -> str:
        rel = str(args.get("path") or "")
        path = (root / rel).resolve()
        if not str(path).startswith(str(root.resolve())) or not path.is_file():
            return '{"error": "not found"}'
        text = path.read_text(encoding="utf-8", errors="replace")
        # Per-owner scope (#576): a path is not a capability — a caller may only
        # read their own or shared notes, never another resident's private note.
        # Deny indistinguishably from a missing path (no content, no existence).
        if not notes_search.is_visible(notes_search.owner_of(text), uid_getter()):
            return '{"error": "not found"}'
        return json.dumps({"path": rel, "content": text[:8000]}, ensure_ascii=False)

    async def write(args: dict[str, Any]) -> str:
        rel = str(args.get("path") or "").strip().lstrip("/")
        content = str(args.get("content") or "")
        if not rel.endswith(".md") or not content.strip():
            return '{"error": "path must end in .md and content must be non-empty"}'
        path = (root / rel).resolve()
        if not str(path).startswith(str(root.resolve())):
            return '{"error": "path outside the vault"}'
        path.parent.mkdir(parents=True, exist_ok=True)
        if bool(args.get("append")) and path.is_file():
            with path.open("a", encoding="utf-8") as f:
                f.write("\n" + content.rstrip("\n") + "\n")
        else:
            # Stamp the caller as owner (#576): a model-written note belongs to
            # the resident it was written for, not the shared pool. Without this
            # the note is untagged and (None = shared) visible to everyone.
            body = content.rstrip("\n") + "\n"
            owner = uid_getter()
            note = f"---\nadded_by: {owner}\n---\n\n{body}"
            path.write_text(note, encoding="utf-8")
        return json.dumps({"written": rel})

    async def fact_store(args: dict[str, Any]) -> str:
        fact = str(args.get("fact") or "").strip()
        if not fact:
            return '{"error": "empty fact"}'
        facts_dir = root / "facts"
        facts_dir.mkdir(parents=True, exist_ok=True)
        slug = re.sub(r"[^a-z0-9äöüß]+", "-", fact.lower())[:48].strip("-")
        day = datetime.now(UTC).strftime("%Y-%m-%d")
        path = facts_dir / f"{day}-{slug or 'fact'}.md"
        path.write_text(
            f"---\nadded_by: {uid_getter()}\ndate: {day}\n---\n\n{fact}\n",
            encoding="utf-8",
        )
        return json.dumps({"stored": str(path.relative_to(root))})

    return [
        Tool(
            name="notes_search",
            description="Durchsucht die Haushalts-Notizen (Stichwortsuche).",
            parameters={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
            handler=search,
        ),
        Tool(
            name="notes_read",
            description="Liest eine Notiz (Pfad aus notes_search).",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
            handler=read,
        ),
        Tool(
            name="note_write",
            description=(
                "Schreibt eine Notiz in den Haushalts-Vault (Markdown)."
                " append=true hängt an eine bestehende Notiz an."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "relativ, endet .md"},
                    "content": {"type": "string"},
                    "append": {"type": "boolean"},
                },
                "required": ["path", "content"],
            },
            handler=write,
        ),
        Tool(
            name="fact_store",
            description=(
                "Speichert einen dauerhaften Fakt über den Haushalt"
                " (Vorlieben, Routinen, Personen — keine Gerätezustände)."
            ),
            parameters={
                "type": "object",
                "properties": {"fact": {"type": "string"}},
                "required": ["fact"],
            },
            handler=fact_store,
        ),
    ]

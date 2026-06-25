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
from solaris_chat.engine.fuzzy import fuzzy_score, tokens
from solaris_chat.engine.tools import Tool
from solaris_chat.notes_search import _USER_PATH_RE

_MAX_BYTES = 256 * 1024
_MAX_HITS = 8

# Ranked-search blend (#591): the short title is fuzzily matched (typos clear),
# the body contributes a cheap whole-word coverage of the query terms (NO
# SequenceMatcher over full bodies). `_MIN_SCORE` is tuned so a note carrying all
# query terms as body words always clears it (today's exact-AND matches stay a
# superset at the top); only strong partials additionally leak through.
_TITLE_WEIGHT = 0.6
_BODY_WEIGHT = 0.4
_ALL_TERMS_BONUS = 0.1
_MIN_SCORE = 0.2


def _snippet(text: str, present_terms: list[str]) -> str:
    """A ~240-char excerpt anchored on the first present query term, else start."""
    lower = text.lower()
    idx = -1
    for term in present_terms:
        idx = lower.find(term)
        if idx != -1:
            break
    if idx == -1:
        idx = 0
    return text[max(0, idx - 80) : idx + 160].replace("\n", " ")


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
        terms = tokens(query)
        if not terms:
            return "[]"
        scored: list[tuple[float, dict[str, Any]]] = []
        for path in sorted(root.rglob("*.md")):
            try:
                if not path.is_file() or path.stat().st_size > _MAX_BYTES:
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            rel = str(path.relative_to(root))
            # Scope BEFORE scoring (#576): never score what the caller can't see.
            if not notes_search.is_visible(
                notes_search.owner_of(rel, text), caller_uid
            ):
                continue
            title = notes_search._title(text, path.stem)
            # Body contribution: whole-word coverage of the query terms over a SET
            # of the body's word-tokens — cheap, no SequenceMatcher over the body.
            body_words = set(tokens(text))
            present = [t for t in terms if t in body_words]
            body_coverage = len(present) / len(terms)
            if len(present) == len(terms):
                body_coverage += _ALL_TERMS_BONUS
            score = _TITLE_WEIGHT * fuzzy_score(query, title) + _BODY_WEIGHT * (
                body_coverage
            )
            if score < _MIN_SCORE:
                continue
            snippet = _snippet(text, present)
            scored.append(
                (
                    score,
                    {
                        "path": rel,
                        "title": title,
                        "snippet": snippet,
                        "score": round(score, 4),
                    },
                )
            )
        scored.sort(key=lambda s: s[0], reverse=True)
        return json.dumps([h for _, h in scored[:_MAX_HITS]], ensure_ascii=False)

    async def read(args: dict[str, Any]) -> str:
        rel = str(args.get("path") or "")
        path = (root / rel).resolve()
        if not str(path).startswith(str(root.resolve())) or not path.is_file():
            return '{"error": "not found"}'
        text = path.read_text(encoding="utf-8", errors="replace")
        # Per-owner scope (#576): a path is not a capability — a caller may only
        # read their own or shared notes, never another resident's private note.
        # Ownership is path-based (users/<uid>/) then frontmatter; use the
        # vault-relative resolved path so `../`-style args can't dodge the prefix.
        canon = str(path.relative_to(root.resolve()))
        if not notes_search.is_visible(
            notes_search.owner_of(canon, text), uid_getter()
        ):
            return '{"error": "not found"}'
        return json.dumps({"path": rel, "content": text[:8000]}, ensure_ascii=False)

    async def write(args: dict[str, Any]) -> str:
        rel = str(args.get("path") or "").strip().lstrip("/")
        content = str(args.get("content") or "")
        if not rel.endswith(".md") or not content.strip():
            return '{"error": "path must end in .md and content must be non-empty"}'
        # Private-by-default (#576): a real resident's writes ALWAYS land inside
        # their own `users/<owner>/` subtree — a model-supplied `users/<other>/x`
        # or a `../<other>/x` traversal must never reach another resident's space.
        # Strip any leading `users/<anyuid>/`, neutralise `..`, then re-root under
        # the caller; household keeps writing to the shared vault root.
        owner = uid_getter()
        if owner != notes_search.SHARED_UID:
            safe_rel = _USER_PATH_RE.sub("", rel)
            base = (root / "users" / owner).resolve()
            path = (base / safe_rel).resolve()
            if not str(path).startswith(str(base) + "/"):
                return '{"error": "path outside the vault"}'
            rel = str(path.relative_to(root.resolve()))
        else:
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
            note = f"---\nadded_by: {owner}\n---\n\n{body}"
            path.write_text(note, encoding="utf-8")
        return json.dumps({"written": rel})

    async def fact_store(args: dict[str, Any]) -> str:
        fact = str(args.get("fact") or "").strip()
        if not fact:
            return '{"error": "empty fact"}'
        # Private-by-default (#576): a resident's facts live under their own path;
        # household facts stay in the shared `facts/` dir.
        owner = uid_getter()
        facts_dir = root / "facts"
        if owner != notes_search.SHARED_UID:
            facts_dir = root / "users" / owner / "facts"
        facts_dir.mkdir(parents=True, exist_ok=True)
        slug = re.sub(r"[^a-z0-9äöüß]+", "-", fact.lower())[:48].strip("-")
        day = datetime.now(UTC).strftime("%Y-%m-%d")
        path = facts_dir / f"{day}-{slug or 'fact'}.md"
        path.write_text(
            f"---\nadded_by: {owner}\ndate: {day}\n---\n\n{fact}\n",
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

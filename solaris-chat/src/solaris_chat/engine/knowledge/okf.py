"""OKF concept-file (de)serialization (docs/okf-write-contract.md §3).

One concept = one `.md` with YAML-ish frontmatter + body + an optional
`## Relationships` section (``- <rel> → [[<path>]]`` lines). We hand-render a
small, deterministic frontmatter subset (no PyYAML dependency in the engine) so
the output is stable and the content_hash only moves on real content change.
"""

from __future__ import annotations

import hashlib
from typing import Any

from .records import ConceptRecord, Relationship, domain_for
from .slug import safe_slug


_REL_ARROW = "→"


_SHARED_RESIDENT = "household"


def deterministic_id(rel_path: str) -> str:
    """A stable 32-hex id derived from the (deterministic) OKF path.

    A projection-only event (an Immich photo) has no `concepts` row to dedup
    against, so it keys its id off this instead of a random uuid — the same asset
    re-ingests to the same event id, so the event isn't duplicated and its
    content_hash (which carries the id) doesn't churn."""
    return hashlib.sha256(rel_path.encode("utf-8")).hexdigest()[:32]


def okf_path(record: ConceptRecord) -> str:
    """`okf/<domain>/<slug>.md` — events are date-prefixed and year-sharded (§2).

    Events land under `okf/events/<year>/<slug>.md` (#830): a flat `events/` dir
    grew to ~76k immich notes, so we shard by the asset's year (from `event_ts`).
    Path-based ownership (#576): a concept owned by a real resident lands under
    `users/<resident>/okf/...` (private to them); household stays shared at the
    vault-root `okf/...`."""
    domain = domain_for(record.type)
    slug = safe_slug(record.slug or record.title)
    if record.type == "event":
        day = (record.event_ts or record.timestamp or "")[:10]
        if day:
            slug = f"{safe_slug(day)}-{slug}"
        year = day[:4]
        rel = f"okf/{domain}/{year}/{slug}.md" if year else f"okf/{domain}/{slug}.md"
    else:
        rel = f"okf/{domain}/{slug}.md"
    if record.resident and record.resident != _SHARED_RESIDENT:
        return f"users/{safe_slug(record.resident)}/{rel}"
    return rel


def _scalar(value: Any) -> str:
    return str(value).strip()


def _frontmatter(record: ConceptRecord, *, entity_id: str) -> list[str]:
    """The ordered frontmatter lines. `type`/`id`/`resident`/`source` are
    required (§3); the rest are emitted only when present so the hash is stable.
    """
    lines: list[str] = ["---"]
    lines.append(f"type: {record.type}")
    lines.append(f"id: {entity_id}")
    if record.title:
        lines.append(f"title: {_scalar(record.title)}")
    if record.description:
        lines.append(f"description: {_scalar(record.description)}")
    lines.append(f"resident: {record.resident}")
    lines.append(f"source: {record.source}")
    if record.timestamp:
        lines.append(f"timestamp: {record.timestamp}")
    if record.resource:
        lines.append(f"resource: {record.resource}")
    for key, value in record.extra.items():
        if isinstance(value, list):
            lines.append(f"{key}:")
            for item in value:
                lines.append(f"  - {_scalar(item)}")
        else:
            lines.append(f"{key}: {_scalar(value)}")
    for fact in record.facts:
        predicate, value = fact[0], fact[1]
        lines.append(f"{predicate}: {_scalar(value)}")
    if record.aliases:
        lines.append("aliases:")
        for alias in record.aliases:
            lines.append(f"  - {_scalar(alias)}")
    if record.tags:
        lines.append("tags:")
        for tag in record.tags:
            lines.append(f"  - {_scalar(tag)}")
    lines.append("---")
    return lines


def _relationships(rels: list[Relationship]) -> list[str]:
    if not rels:
        return []
    lines = ["", "## Relationships", ""]
    for r in rels:
        lines.append(f"- {r.rel} {_REL_ARROW} [[{r.path}]]")
    return lines


def render(record: ConceptRecord, *, entity_id: str) -> str:
    """The full OKF concept-file text for `record`."""
    parts = _frontmatter(record, entity_id=entity_id)
    body = record.body.strip("\n")
    if body:
        parts.extend(["", body])
    parts.extend(_relationships(record.relationships))
    return "\n".join(parts).rstrip("\n") + "\n"


def content_hash(text: str) -> str:
    """Stable hash of the rendered concept file — the re-ingest skip key (§5)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def read_concept(text: str) -> dict[str, str]:
    """Parse a stored OKF file's `description` (from frontmatter) + `body`.

    The concept page (#502) shows what was authored, not the whole file: the
    `description:` frontmatter line and the prose body (everything after the
    closing `---`, with a trailing `## Relationships` section dropped — those
    are rendered as their own links, not body text). Tolerant of a missing
    frontmatter fence so a hand-written note still yields a body.
    """
    lines = text.splitlines()
    description = ""
    body_start = 0
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                body_start = i + 1
                break
            m = lines[i].split(":", 1)
            if len(m) == 2 and m[0].strip() == "description":
                description = m[1].strip()
    body_lines = lines[body_start:]
    for i, line in enumerate(body_lines):
        if line.strip().startswith("## Relationships"):
            body_lines = body_lines[:i]
            break
    return {"description": description, "body": "\n".join(body_lines).strip()}

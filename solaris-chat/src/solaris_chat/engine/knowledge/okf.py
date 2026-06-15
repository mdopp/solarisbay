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


def okf_path(record: ConceptRecord) -> str:
    """`notes/okf/<domain>/<slug>.md` — events are date-prefixed (§2)."""
    domain = domain_for(record.type)
    slug = safe_slug(record.slug or record.title)
    if record.type == "event":
        day = (record.event_ts or record.timestamp or "")[:10]
        if day:
            slug = f"{safe_slug(day)}-{slug}"
    return f"okf/{domain}/{slug}.md"


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

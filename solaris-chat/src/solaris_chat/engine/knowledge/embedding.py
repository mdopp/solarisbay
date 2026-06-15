"""Whole-concept embedding enqueue (docs/okf-write-contract.md §5).

Policy: one vector per concept (title + description + body), model
`nomic-embed-text`, stored keyed by `concepts.embedding_id`, (re-)embedded only
when `content_hash` changed.

There is **no vector/episodic store wired into the engine yet** — `engine/ollama.py`
exposes only `/api/chat`, `/api/tags`, `/api/ps`, `/api/pull`, no `/api/embeddings`,
and there is no holographic store to key into. Rather than fake a vector store,
the writer enqueues the embedding work through this small interface. The default
`PendingEmbeddingQueue` durably records the pending `(embedding_id, concept_id,
text)` triples to a JSON sidecar next to `solaris.db`; the actual vectorization
(call `nomic-embed-text`, persist the vector) is a TODO for the embedding worker
once that store exists. `enqueue` returns the `embedding_id` to store on the
`concepts` row.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol


class EmbeddingQueue(Protocol):
    def enqueue(self, *, concept_id: str, embedding_id: str, text: str) -> str:
        """Enqueue a whole-concept (re-)embedding; return the `embedding_id`."""
        ...


class NullEmbeddingQueue:
    """Drops the work — for tests/adapters that don't exercise embedding."""

    def enqueue(self, *, concept_id: str, embedding_id: str, text: str) -> str:
        return embedding_id


class PendingEmbeddingQueue:
    """Durably records pending embeddings to a JSON sidecar.

    Keyed by `embedding_id` so a re-embed of the same concept overwrites the
    prior pending entry instead of duplicating it. A real worker will drain
    this, call `nomic-embed-text`, store the vector, and clear the entry.
    """

    def __init__(self, db_path: str):
        self._path = Path(db_path).with_name("okf_embedding_queue.json")

    def enqueue(self, *, concept_id: str, embedding_id: str, text: str) -> str:
        pending = self._load()
        pending[embedding_id] = {
            "concept_id": concept_id,
            "model": "nomic-embed-text",
            "text": text,
        }
        self._path.write_text(
            json.dumps(pending, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        # TODO(okf-embed): a worker drains this, calls nomic-embed-text, stores
        # the vector in the (not-yet-existing) episodic/holographic store keyed
        # by embedding_id, then removes the entry. Wire it when the store lands.
        return embedding_id

    def _load(self) -> dict[str, dict[str, str]]:
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

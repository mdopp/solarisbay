"""OKF knowledge write-path core.

The shared writer every ingestion adapter (Immich/calendar/contacts/Obsidian)
calls. An adapter normalizes its source into a `ConceptRecord` and hands it to
`write_concept`; the writer owns the four sinks the OKF write contract defines
(docs/okf-write-contract.md §3–§6):

  1. resolve/create the entity (alias dedup via `entity_aliases` + `ingest_log`);
  2. write/update the OKF concept `.md` under `notes/okf/<domain>/<slug>.md`;
  3. update the rebuildable `.db` projection (entities/facts/events/
     event_entities/concepts, #446);
  4. enqueue a whole-concept embedding (re-embed only when `content_hash`
     changed);
  5. record `ingest_log` idempotently (unchanged hash → skip).

OKF files are the source of truth; the `.db` rows + embeddings are a derived
projection.
"""

from __future__ import annotations

from .embedding import EmbeddingQueue, NullEmbeddingQueue, PendingEmbeddingQueue
from .records import ConceptRecord, Relationship, WriteResult
from .slug import safe_slug
from .writer import OkfWriter, write_concept


__all__ = [
    "ConceptRecord",
    "Relationship",
    "WriteResult",
    "OkfWriter",
    "write_concept",
    "safe_slug",
    "EmbeddingQueue",
    "NullEmbeddingQueue",
    "PendingEmbeddingQueue",
]

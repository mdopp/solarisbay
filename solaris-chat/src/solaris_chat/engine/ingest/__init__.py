"""Ingestion adapters — the OKF write-path producers.

Each adapter reads its source **read-only** and normalizes records into the
shared OKF writer (`engine.knowledge.write_concept`). Adapters never read
`gbrain`; they only write (docs/okf-write-contract.md §6).
"""

from __future__ import annotations

from .immich import ImmichIngest, ImmichIngestStats
from .immich_client import ImmichAsset, ImmichClient, ImmichPerson, RestImmichClient


__all__ = [
    "ImmichIngest",
    "ImmichIngestStats",
    "ImmichAsset",
    "ImmichPerson",
    "ImmichClient",
    "RestImmichClient",
]

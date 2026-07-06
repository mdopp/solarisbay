"""Drain the OKF embedding queue into the `okf_vectors` store (#650).

`PendingEmbeddingQueue` (embedding.py) appends one JSONL line per (re-)embed to
`okf_embedding_queue.jsonl` next to `solaris.db`; the same `embedding_id` may
repeat (a re-embed appends), **last line wins**. This worker consumes those
lines, calls `nomic-embed-text`, and upserts one float32 vector per
`embedding_id` into `okf_vectors`.

`drain()` is a plain async function invoked from the tail of `run_ingest()` (no
new thread/task/knob): that single call site covers "at boot" and "after every
ingest run", and the nightly pipeline (#652) re-runs `run_ingest()` for
"periodically". It must never run on the voice hot path — the box caps
`OLLAMA_MAX_LOADED_MODELS=2` and `nomic-embed-text` counts as a full slot.

Note: `okf_vectors.concept_id` carries the writer's `ref_id` (the entity/event
id), NOT `concepts.id`. Retrieval joins through
`concepts.embedding_id = okf_vectors.embedding_id`.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import numpy as np

from solaris_chat.logging import log

from ..ollama import OllamaChat, OllamaError
from . import projection

_MODEL = "nomic-embed-text"
_BATCH = 64


async def drain(db_path: str, ollama_url: str) -> None:
    """Consume the embedding queue into `okf_vectors`. Never raises.

    Crash-safe: the live queue is `os.rename`d to a `.draining` sidecar (atomic;
    writers append to a fresh queue thereafter) and processed from there, so a
    crash mid-drain resumes from the `.draining` file on the next run rather than
    losing lines. If `nomic-embed-text` can't be ensured, the `.draining` file is
    left in place and the next drain retries it.
    """
    try:
        queue_path = Path(db_path).with_name("okf_embedding_queue.jsonl")
        draining_path = queue_path.with_name(queue_path.name + ".draining")

        # A crashed drain left older lines in `.draining`; read them into memory
        # FIRST (renaming the live queue over the path would clobber the file),
        # then move the live queue aside so writers append to a fresh file. Order
        # matters for last-line-wins: the resumed lines are older than the queue.
        blocks: list[str] = []
        if draining_path.exists():
            blocks.append(draining_path.read_text(encoding="utf-8"))
        if queue_path.exists():
            os.rename(queue_path, draining_path)
            blocks.append(draining_path.read_text(encoding="utf-8"))

        if not blocks:
            return

        entries: dict[str, dict] = {}
        skipped = 0
        for block in blocks:
            for raw in block.splitlines():
                line = raw.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    entries[entry["embedding_id"]] = entry
                except (json.JSONDecodeError, KeyError, TypeError):
                    skipped += 1

        if not entries:
            draining_path.unlink(missing_ok=True)
            return

        client = OllamaChat(ollama_url)
        if not await _ensure_model(client):
            log.warning("engine.embed.model_missing", pending=len(entries))
            return  # leave .draining in place; next drain resumes it.

        items = list(entries.values())
        conn = projection.open_conn(db_path)
        try:
            drained = 0
            for start in range(0, len(items), _BATCH):
                batch = items[start : start + _BATCH]
                vectors = await client.embed(_MODEL, [e["text"] for e in batch])
                for entry, vec in zip(batch, vectors, strict=True):
                    blob = np.asarray(vec, dtype=np.float32).tobytes()
                    conn.execute(
                        """
                        INSERT INTO okf_vectors
                          (embedding_id, concept_id, model, dim, vector)
                        VALUES (?, ?, ?, ?, ?)
                        ON CONFLICT(embedding_id) DO UPDATE SET
                          concept_id = excluded.concept_id,
                          model = excluded.model,
                          dim = excluded.dim,
                          vector = excluded.vector,
                          updated = datetime('now')
                        """,
                        (
                            entry["embedding_id"],
                            entry["concept_id"],
                            entry.get("model") or _MODEL,
                            len(vec),
                            blob,
                        ),
                    )
                conn.commit()
                drained += len(batch)
        finally:
            conn.close()

        draining_path.unlink(missing_ok=True)
        log.info("engine.embed.drained", drained=drained, skipped=skipped)
    except Exception as e:  # noqa: BLE001 — the drain must never crash the ingest.
        log.error("engine.embed.drain_failed", error=str(e))


async def _ensure_model(client: OllamaChat) -> bool:
    """True once `nomic-embed-text` is present; try one pull if it's absent.
    False (Ollama unreachable / pull failed) leaves the queue for a retry."""
    try:
        tags = await client.tags()
        if any((t.get("name") or "").startswith(_MODEL) for t in tags):
            return True
        log.info("engine.embed.pull_start", model=_MODEL)
        async for _ in client.pull(_MODEL):
            pass
        log.info("engine.embed.pull_done", model=_MODEL)
        return True
    except (OllamaError, OSError):
        return False

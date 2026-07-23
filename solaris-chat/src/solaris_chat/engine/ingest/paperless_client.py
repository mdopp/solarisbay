"""Thin paperless-ngx REST client for the document push adapter (#931).

Paperless is a document store + Web-UI + full-text search only; its own
Tesseract OCR is skipped (the #929 PoC proved it garbles rotated German scans).
So the handoff is: POST the file (paperless consumes it OCR-skipped), resolve
the created document id from the consume task, then PATCH `content` with the
clean gemma4:12b vision text so full-text search indexes the good text.

Auth is a paperless API token on the host loopback (bypasses forward-auth).
The adapter depends on the `PaperlessClient` Protocol so tests inject a fake and
the live path uses `RestPaperlessClient` (aiohttp, like the Immich client).
"""

from __future__ import annotations

import asyncio
from typing import Any, Protocol

import aiohttp

from ...logging import log

# post_documents is async on paperless: the POST returns a consume-task UUID, the
# document id only exists once the worker finishes. Poll the task a bounded number
# of times before giving up (a huge scan can take a few seconds to consume).
_TASK_POLL_BACKOFF = (0.5, 1.0, 2.0, 3.0, 5.0)  # seconds — ~11.5s total.


class PaperlessClient(Protocol):
    """The paperless write path the adapter needs. Injectable for tests."""

    async def post_document(self, file_bytes: bytes, filename: str) -> int | None:
        """Consume a file OCR-skipped; return the created document id (or None
        if paperless dropped it as a duplicate / the task didn't finish)."""
        ...

    async def patch_content(self, document_id: int, content: str) -> None:
        """Replace the document's full-text `content` so search re-indexes it."""
        ...


class RestPaperlessClient:
    """aiohttp wrapper over the paperless-ngx REST API (token auth)."""

    def __init__(self, base_url: str, token: str, *, timeout: float = 60.0):
        self._base_url = base_url.rstrip("/")
        self._headers = {
            "Authorization": f"Token {token}",
            "Accept": "application/json",
        }
        self._timeout = aiohttp.ClientTimeout(total=timeout)

    async def post_document(self, file_bytes: bytes, filename: str) -> int | None:
        form = aiohttp.FormData()
        form.add_field("document", file_bytes, filename=filename)
        async with aiohttp.ClientSession(timeout=self._timeout) as client:
            async with client.post(
                f"{self._base_url}/api/documents/post_document/",
                data=form,
                headers=self._headers,
            ) as resp:
                resp.raise_for_status()
                task_id = (
                    (await resp.json()) if resp.content_length else await resp.text()
                )
            return await self._resolve_document_id(client, str(task_id).strip('"'))

    async def _resolve_document_id(
        self, client: aiohttp.ClientSession, task_id: str
    ) -> int | None:
        """Poll the consume task until it reports the created document id.

        Returns None when the task finishes without one (paperless dedups a
        re-upload to an existing doc) or never completes in the poll budget."""
        for delay in _TASK_POLL_BACKOFF:
            await asyncio.sleep(delay)
            async with client.get(
                f"{self._base_url}/api/tasks/",
                params={"task_id": task_id},
                headers=self._headers,
            ) as resp:
                resp.raise_for_status()
                tasks = await resp.json()
            task = tasks[0] if tasks else {}
            status = task.get("status")
            if status == "SUCCESS":
                doc_id = task.get("related_document")
                return int(doc_id) if doc_id else None
            if status == "FAILURE":
                log.error("engine.ingest.paperless_task_failed", task=task_id)
                return None
        log.info("engine.ingest.paperless_task_pending", task=task_id)
        return None

    async def patch_content(self, document_id: int, content: str) -> None:
        body: dict[str, Any] = {"content": content}
        async with aiohttp.ClientSession(timeout=self._timeout) as client:
            async with client.patch(
                f"{self._base_url}/api/documents/{document_id}/",
                json=body,
                headers=self._headers,
            ) as resp:
                resp.raise_for_status()

"""Proxy-side persistence for chat image attachments (#202).

The chat surface is otherwise stateless — all chat/session state lives in
Hermes. But Hermes does not retain inbound images: a user turn carrying image
parts is persisted as text with a `[screenshot]` placeholder, and Hermes
exposes no attachment API to read the pixels back. So after a hard refresh the
attachment is gone from history.

This module is the one exception to the stateless rule: it persists the image
data URLs the browser sent, keyed by session id, in a small JSON file per
session under a host-mounted data dir. On history load the proxy walks the
session's user messages in order; each message bearing the `[screenshot]`
placeholder consumes the next stored batch, re-attaching the data URLs so the
UI re-renders the thumbnails.

Storage is best-effort: a failed read/write logs and degrades to "no stored
attachments" rather than failing the turn — an attachment is a nicety, the
reply is the contract.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from solilos_chat.logging import log

# A persisted user turn that carried images shows up in Hermes' message
# history with this placeholder (Hermes substitutes it for every image part).
SCREENSHOT_PLACEHOLDER = "[screenshot]"
# Hermes ids are `api_<ts>_<hex>`; map everything else (including `.` and `/`)
# to `_` so a crafted id can't form a `..`/path component and escape the root.
_SAFE_ID = re.compile(r"[^A-Za-z0-9_-]")


class AttachmentStore:
    """Per-session image-attachment store backed by one JSON file per session.

    File shape: ``{"batches": [[<data-url>, ...], ...]}`` — one batch per turn
    that carried images, in chronological (append) order.
    """

    def __init__(self, root: str) -> None:
        self._root = Path(root)

    def _path(self, session_id: str) -> Path:
        # Hermes session ids are `api_<ts>_<hex>`; sanitise defensively so a
        # crafted id can't escape the store dir.
        safe = _SAFE_ID.sub("_", session_id)
        return self._root / f"{safe}.json"

    def add(self, session_id: str, images: list[str]) -> None:
        """Append one batch of image data URLs for a session turn."""
        if not session_id or not images:
            return
        try:
            self._root.mkdir(parents=True, exist_ok=True)
            path = self._path(session_id)
            batches = self._load(path)
            batches.append(list(images))
            path.write_text(json.dumps({"batches": batches}), encoding="utf-8")
        except OSError as e:
            log.error(
                "chat.attachments.write_failed", session_id=session_id, error=str(e)
            )

    def batches(self, session_id: str) -> list[list[str]]:
        """All stored image batches for a session, in turn order."""
        if not session_id:
            return []
        return self._load(self._path(session_id))

    def delete(self, session_id: str) -> None:
        """Drop a session's stored attachments (best-effort)."""
        if not session_id:
            return
        try:
            self._path(session_id).unlink(missing_ok=True)
        except OSError as e:
            log.error(
                "chat.attachments.delete_failed", session_id=session_id, error=str(e)
            )

    @staticmethod
    def _load(path: Path) -> list[list[str]]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as e:
            log.error("chat.attachments.read_failed", path=str(path), error=str(e))
            return []
        batches = data.get("batches") if isinstance(data, dict) else None
        if not isinstance(batches, list):
            return []
        return [b for b in batches if isinstance(b, list)]


def attach_to_messages(
    messages: list[dict[str, str]], batches: list[list[str]]
) -> None:
    """Re-attach stored image batches to the user messages that carried them.

    Walks `messages` in order; each user message whose content bears the
    `[screenshot]` placeholder consumes the next batch (in chronological order),
    adding an `images` key with the stored data URLs. Mutates `messages` in
    place. Surplus batches (more stored than placeholders found) are ignored —
    the rendered history simply shows what can be correlated.
    """
    if not batches:
        return
    it = iter(batches)
    for m in messages:
        if m.get("role") != "user" or SCREENSHOT_PLACEHOLDER not in (
            m.get("content") or ""
        ):
            continue
        batch = next(it, None)
        if batch is None:
            break
        m["images"] = batch

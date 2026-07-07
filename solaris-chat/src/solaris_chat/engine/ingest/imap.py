"""IMAP email ingest adapter (#654, docs/okf-write-contract.md §3).

Reads one curated IMAP folder per account **read-only** and writes each mail as
an OKF `event` concept (kind `email`) via the shared #447 writer. The folder is
the structural filter (§3.5): the adapter only ever selects that folder
read-only, so the user curates what Solaris sees by labeling/moving mail — no
per-account content knob. Each account maps to exactly one `resident_uid`, so a
mail lands under `users/<uid>/okf/events/...` by construction (§3.6).

Body = the mail's plain text verbatim (text/plain preferred, HTML stripped as a
fallback); distillation/entity-resolution is the Bibliothekar's job (#653), not
the adapter's.

Idempotent + incremental: every write goes through the writer's `ingest_log`
(`source="imap"`, the per-mail external_id) + `content_hash`, so a re-run with an
unchanged mail is a no-op. A per-account+folder cursor (`<uidvalidity>:<last_uid>`)
means a re-run doesn't even fetch already-seen mail.
"""

from __future__ import annotations

import email
import email.policy
import email.utils
import html
import imaplib
import re
from collections.abc import Callable
from dataclasses import dataclass

from ...config import ImapAccount
from ...logging import log
from ..knowledge import ConceptRecord
from ..knowledge.writer import OkfWriter


_SOURCE = "imap"

# Body cap: HTML-only newsletters produce huge noisy bodies; the plain-text
# preference plus this cap is the whole v1 answer (no readability library).
_MAX_BODY = 32 * 1024

# Checkpoint the cursor every N mails so a disconnect after N fetches still
# advances the high-water mark and the next run resumes (#597).
_CHECKPOINT_EVERY = 25

_TAG_RE = re.compile(r"<[^>]+>")


@dataclass
class ImapIngestStats:
    account: str = ""
    seen: int = 0
    written: int = 0
    skipped: int = 0
    # `<uidvalidity>:<last_uid>` high-water cursor for the next incremental run.
    cursor: str = ""


class ImapIngest:
    def __init__(self, writer: OkfWriter):
        self._writer = writer

    def run_account(
        self,
        account: ImapAccount,
        cursor: str,
        *,
        checkpoint: Callable[[str], None] | None = None,
    ) -> ImapIngestStats:
        """Ingest new mail in `account`'s folder since `cursor`; return stats.

        Synchronous (imaplib blocks) — the runner wraps this in
        `asyncio.to_thread`. The cursor is `<uidvalidity>:<last_uid>`; on a
        UIDVALIDITY change the server has renumbered UIDs, so `last_uid` resets
        to 0 and the folder is re-walked (the content_hash still dedups it).
        """
        label = f"{account.username}@{account.host}/{account.folder}"
        stats = ImapIngestStats(account=label, cursor=cursor)

        conn = imaplib.IMAP4_SSL(account.host, account.port)
        try:
            conn.login(account.username, account.password)
            conn.select(account.folder, readonly=True)

            uidvalidity = self._uidvalidity(conn)
            last = self._last_uid(cursor, uidvalidity)
            stats.cursor = f"{uidvalidity}:{last}"
            last_checkpointed = stats.cursor

            for uid in self._new_uids(conn, last):
                try:
                    self._ingest_mail(conn, account, uidvalidity, uid, stats)
                except Exception as e:  # noqa: BLE001
                    # One bad mail (e.g. a title safe_slug chokes on) must never
                    # abort the folder (#528).
                    log.error(
                        "engine.ingest.imap_mail_failed",
                        account=label,
                        uid=uid,
                        error=str(e),
                    )
                    stats.skipped += 1
                stats.seen += 1
                stats.cursor = f"{uidvalidity}:{uid}"
                if (
                    checkpoint is not None
                    and stats.seen % _CHECKPOINT_EVERY == 0
                    and stats.cursor != last_checkpointed
                ):
                    checkpoint(stats.cursor)
                    last_checkpointed = stats.cursor
        finally:
            try:
                conn.logout()
            except Exception:  # noqa: BLE001 — logout failure must not mask the run.
                pass
        return stats

    def _uidvalidity(self, conn: imaplib.IMAP4_SSL) -> int:
        _, data = conn.response("UIDVALIDITY")
        if data and data[0]:
            return int(data[0])
        return 0

    def _last_uid(self, cursor: str, uidvalidity: int) -> int:
        if not cursor or ":" not in cursor:
            return 0
        stored_validity, _, stored_uid = cursor.partition(":")
        # UIDVALIDITY change ⇒ the server renumbered UIDs; the old high-water is
        # meaningless — re-walk from 0 (content_hash dedups the re-walk).
        if stored_validity != str(uidvalidity):
            return 0
        try:
            return int(stored_uid)
        except ValueError:
            return 0

    def _new_uids(self, conn: imaplib.IMAP4_SSL, last: int) -> list[int]:
        typ, data = conn.uid("search", None, "UID", f"{last + 1}:*")
        if typ != "OK" or not data or not data[0]:
            return []
        # `n:*` always returns at least the last message even when its UID < n
        # (an empty range still yields the last mail) — filter uid > last.
        uids = sorted(int(tok) for tok in data[0].split())
        return [u for u in uids if u > last]

    def _ingest_mail(
        self,
        conn: imaplib.IMAP4_SSL,
        account: ImapAccount,
        uidvalidity: int,
        uid: int,
        stats: ImapIngestStats,
    ) -> None:
        typ, data = conn.uid("fetch", str(uid), "(RFC822)")
        if typ != "OK" or not data or not isinstance(data[0], tuple):
            stats.skipped += 1
            return
        raw = data[0][1]
        msg = email.message_from_bytes(raw, policy=email.policy.default)

        subject = str(msg["subject"] or "").strip() or "(kein Betreff)"
        from_header = str(msg["from"] or "").strip()
        when = self._when(msg)
        body = self._body(msg)

        external_id = (
            f"{account.username}@{account.host}/{account.folder}/{uidvalidity}/{uid}"
        )
        rec = ConceptRecord(
            type="event",
            title=subject,
            source=_SOURCE,
            external_id=external_id,
            resident=account.resident_uid,
            timestamp=when,
            event_ts=when,
            event_kind="email",
            extra={"when": when, "from": from_header, "subject": subject},
            body=body,
        )
        if self._writer.write_concept(rec, ingesting_uid=account.resident_uid).skipped:
            stats.skipped += 1
        else:
            stats.written += 1

    def _when(self, msg: email.message.EmailMessage) -> str:
        raw = msg["date"]
        if raw:
            try:
                dt = email.utils.parsedate_to_datetime(str(raw))
                if dt is not None:
                    return dt.isoformat()
            except (TypeError, ValueError):
                pass
        return ""

    def _body(self, msg: email.message.EmailMessage) -> str:
        part = msg.get_body(preferencelist=("plain", "html"))
        if part is None:
            return ""
        content = part.get_content()
        if part.get_content_type() == "text/html":
            content = self._html_to_text(content)
        return content[:_MAX_BODY]

    def _html_to_text(self, raw: str) -> str:
        text = _TAG_RE.sub(" ", raw)
        text = html.unescape(text)
        return re.sub(r"[ \t]*\n[ \t\n]*", "\n", text).strip()

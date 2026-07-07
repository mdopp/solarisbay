"""Messenger export drop-folder ingest (#655, docs/okf-write-contract.md §3.5).

A resident drops a chat export (WhatsApp today) into a synced drop folder and
Solaris turns it into dated OKF `event` concepts. The folder is the whole
interface: no external creds, so the adapter always runs (it scans the vault it
already has), like the Obsidian adapter.

Ownership is path-based (§3.6): a file under `notes/users/<uid>/inbox/exports/`
scopes to `<uid>`; `notes/inbox/exports/` is shared/household — resolved by
`notes_search.resident_for_path`.

**Granularity: one OKF event concept per chat per day.** Per-message concepts
would explode a year-long chat into ~100k files; per-file concepts would bury
temporal retrieval (#651 queries `events.ts`). Per-chat-per-day keeps concepts
bounded, gives `events.ts` real resolution, and the day's verbatim transcript in
the body is what semantic search consumes. A `person` concept is written per
unique sender *first* so each event's `with →` edge resolves (the writer drops
unresolved edges silently).

**Parser registry:** `_PARSERS` is a list of `(detect, parse)` pairs keyed on the
filename + first sniffed lines. v1 ships the WhatsApp parser only; Signal and
SMS/RCS-JSON are follow-up issues on this same registry (do not pre-build them).

**Locale trap:** the WhatsApp detect regexes match ONLY `DD.MM.YY` German
exports. A month-first US export would swap day/month and silently corrupt
`events.ts`, so an unrecognized file is left in the inbox and logged, never
mis-parsed.
"""

from __future__ import annotations

import hashlib
import io
import re
import time
import zipfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from ...logging import log
from ...notes_search import resident_for_path
from ..knowledge import ConceptRecord, Relationship, projection, safe_slug
from ..knowledge.writer import OkfWriter


_SOURCE = "exports"

# A day's transcript can be long for a busy chat; cap the body (contract §5
# "chunk only if long" — v1 truncates, no chunking).
_MAX_BODY = 32 * 1024

# Syncthing may still be writing a just-dropped file; a file whose mtime is
# within this window lands on the NEXT run (next boot / nightly #652 run).
_MTIME_GUARD_S = 60

# The scan roots, relative to NOTES_DIR.
_SHARED_ROOT = Path("inbox/exports")
_USERS_GLOB = "users/*/inbox/exports"
_PROCESSED = "processed"

# Invisible marks WhatsApp injects (LRM, narrow NBSP) — strip before matching.
_INVISIBLE = re.compile("[‎ ]")

# `<Medien ausgeschlossen>` — media omitted in v1.
_MEDIA_OMITTED = "<Medien ausgeschlossen>"

# Android: `DD.MM.YY, HH:MM - Name: text`
_ANDROID_RE = re.compile(r"^(\d{1,2})\.(\d{1,2})\.(\d{2,4}), (\d{1,2}):(\d{2}) - (.*)$")
# iOS:     `[DD.MM.YY, HH:MM:SS] Name: text`
_IOS_RE = re.compile(
    r"^\[(\d{1,2})\.(\d{1,2})\.(\d{2,4}), (\d{1,2}):(\d{2}):(\d{2})\] (.*)$"
)


@dataclass
class ExportsIngestStats:
    files: int = 0
    processed: int = 0
    events_written: int = 0
    people_written: int = 0
    skipped: int = 0
    unrecognized: int = 0


@dataclass
class _Message:
    date: str  # ISO YYYY-MM-DD
    time: str  # HH:MM
    sender: str
    text: str


@dataclass
class _Chat:
    """The parsed result of one export file."""

    name: str
    messages: list[_Message] = field(default_factory=list)


class ExportsIngest:
    def __init__(self, writer: OkfWriter, *, db_path: str, notes_dir: str):
        self._writer = writer
        self._db_path = db_path
        self._notes_root = Path(notes_dir)

    def run(self) -> ExportsIngestStats:
        """Scan every drop folder once; return run stats. Never raises per-file."""
        stats = ExportsIngestStats()
        for path in self._scan():
            stats.files += 1
            try:
                self._ingest_file(path, stats)
            except Exception as e:  # noqa: BLE001 — one bad file must not abort the run.
                log.error(
                    "engine.ingest.exports_file_failed",
                    file=str(self._rel(path)),
                    error=str(e),
                )
                stats.skipped += 1
        return stats

    def _scan(self) -> Iterable[Path]:
        """Every export file across the shared + per-user drop folders.

        Skips the `processed/` subtree (matched by path part, not name suffix),
        dotfiles, and files still being written (mtime within the guard window).
        """
        roots = [self._notes_root / _SHARED_ROOT]
        roots += sorted((self._notes_root).glob(_USERS_GLOB))
        now = time.time()
        for root in roots:
            if not root.is_dir():
                continue
            for path in sorted(root.rglob("*")):
                if not path.is_file() or path.name.startswith("."):
                    continue
                if _PROCESSED in path.relative_to(root).parts:
                    continue
                if now - path.stat().st_mtime < _MTIME_GUARD_S:
                    log.info(
                        "engine.ingest.exports_mtime_guard",
                        file=str(self._rel(path)),
                    )
                    continue
                yield path

    def _ingest_file(self, path: Path, stats: ExportsIngestStats) -> None:
        relpath = str(self._rel(path))
        uid = resident_for_path(relpath) or ""

        raw = path.read_bytes()
        # File-level idempotency: a re-dropped identical file is skipped before
        # parsing (the writer also dedups per concept, but this avoids re-work).
        file_hash = _sha256(raw)
        conn = projection.open_conn(self._db_path)
        try:
            if projection.ingest_log_hash(conn, _SOURCE, relpath) == file_hash:
                stats.skipped += 1
                return
        finally:
            conn.close()

        name, text = _text_of(path, raw)
        if text is None:
            log.info(
                "engine.ingest.exports_unrecognized", file=relpath, reason="no-text"
            )
            stats.unrecognized += 1
            return

        first_lines = text.splitlines()[:5]
        chat = None
        for detect, parse in _PARSERS:
            if detect(name, first_lines):
                chat = parse(name, text)
                break
        if chat is None or not chat.messages:
            log.info("engine.ingest.exports_unrecognized", file=relpath)
            stats.unrecognized += 1
            return

        # Person concepts first so the event `with` edges resolve (writer drops
        # unresolved edges silently). Then one event per day.
        senders = {m.sender for m in chat.messages}
        for sender in sorted(senders):
            self._write_person(sender, relpath, uid, stats)
        by_day: dict[str, list[_Message]] = {}
        for m in chat.messages:
            by_day.setdefault(m.date, []).append(m)
        for day in sorted(by_day):
            self._write_day(chat.name, day, by_day[day], relpath, uid, stats)

        # All days written — record the file-level marker and move the file into
        # the sibling `processed/` dir (inside the synced subtree, so the move
        # propagates back to the phone).
        conn = projection.open_conn(self._db_path)
        try:
            projection.record_ingest(
                conn, source=_SOURCE, external_id=relpath, content_hash=file_hash
            )
            conn.commit()
        finally:
            conn.close()
        self._move_processed(path)
        stats.processed += 1

    def _write_person(
        self, name: str, relpath: str, uid: str, stats: ExportsIngestStats
    ) -> None:
        rec = ConceptRecord(
            type="person",
            title=name,
            source=_SOURCE,
            external_id=f"{relpath}:person:{safe_slug(name)}",
            resident=uid,
        )
        if not self._writer.write_concept(rec, ingesting_uid=uid).skipped:
            stats.people_written += 1

    def _write_day(
        self,
        chat: str,
        day: str,
        messages: list[_Message],
        relpath: str,
        uid: str,
        stats: ExportsIngestStats,
    ) -> None:
        senders = sorted({m.sender for m in messages})
        participants = [f"people/{safe_slug(s)}" for s in senders]
        body = "\n".join(f"{m.time} {m.sender}: {m.text}" for m in messages)
        rec = ConceptRecord(
            type="event",
            title=f"WhatsApp {chat} {day}",
            source=_SOURCE,
            external_id=f"{relpath}#{day}",
            resident=uid,
            event_ts=f"{day}T00:00:00",
            event_kind="chat",
            extra={"when": day, "chat": chat, "participants": participants},
            relationships=[Relationship("with", p) for p in participants],
            body=body[:_MAX_BODY],
        )
        if self._writer.write_concept(rec, ingesting_uid=uid).skipped:
            stats.skipped += 1
        else:
            stats.events_written += 1

    def _move_processed(self, path: Path) -> None:
        dest_dir = path.parent / _PROCESSED
        dest_dir.mkdir(parents=True, exist_ok=True)
        path.rename(dest_dir / path.name)

    def _rel(self, path: Path) -> Path:
        return path.resolve().relative_to(self._notes_root.resolve())


# --- WhatsApp parser ----------------------------------------------------------


def _detect_whatsapp(name: str, first_lines: list[str]) -> bool:
    if not (name.endswith(".txt") or name.endswith(".zip")):
        return False
    for line in first_lines:
        stripped = _INVISIBLE.sub("", line)
        if _ANDROID_RE.match(stripped) or _IOS_RE.match(stripped):
            return True
    return False


def _parse_whatsapp(name: str, text: str) -> _Chat:
    chat = _Chat(name=_chat_name(name))
    for raw in text.splitlines():
        line = _INVISIBLE.sub("", raw)
        parsed = _parse_line(line)
        if parsed is None:
            # A line matching no date prefix is a continuation of the previous
            # message (multiline text); nothing to continue → drop it.
            if chat.messages:
                chat.messages[-1].text += "\n" + line
            continue
        day, hhmm, rest = parsed
        sender, sep, msg = rest.partition(": ")
        if not sep:
            continue  # date prefix but no `Name: ` → system message; skip.
        if msg.strip() == _MEDIA_OMITTED:
            continue  # media omitted in v1.
        chat.messages.append(
            _Message(date=day, time=hhmm, sender=sender.strip(), text=msg)
        )
    return chat


def _parse_line(line: str) -> tuple[str, str, str] | None:
    """Return ``(iso_date, HH:MM, rest)`` for a WhatsApp header line, else None."""
    m = _ANDROID_RE.match(line)
    if m:
        d, mo, y, hh, mi, rest = m.groups()
        return _iso(y, mo, d), f"{int(hh):02d}:{mi}", rest
    m = _IOS_RE.match(line)
    if m:
        d, mo, y, hh, mi, _ss, rest = m.groups()
        return _iso(y, mo, d), f"{int(hh):02d}:{mi}", rest
    return None


def _iso(year: str, month: str, day: str) -> str:
    y = int(year)
    if y < 100:
        y += 2000
    return f"{y:04d}-{int(month):02d}-{int(day):02d}"


def _chat_name(name: str) -> str:
    stem = Path(name).stem
    prefix = "WhatsApp Chat mit "
    if stem.startswith(prefix):
        return stem[len(prefix) :]
    if stem == "_chat":
        return "chat"
    return stem


def _text_of(path: Path, raw: bytes) -> tuple[str, str | None]:
    """`(display_name, text)` for an export file; text is None when undecodable.

    A `.zip` is unpacked in memory: read the single `*.txt` member (WhatsApp's
    `_chat.txt`), ignore media members. The display name falls back to the zip
    stem so the chat name survives the `_chat.txt` rename.
    """
    if path.suffix == ".zip":
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as zf:
                members = [n for n in zf.namelist() if n.lower().endswith(".txt")]
                if not members:
                    return path.name, None
                text = zf.read(members[0]).decode("utf-8", "replace")
        except (zipfile.BadZipFile, OSError):
            return path.name, None
        # Name the chat from the zip stem, not the inner `_chat.txt`.
        return f"{path.stem}.txt", text
    return path.name, raw.decode("utf-8", "replace")


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


# The parser registry: `(detect, parse)`. v1 = WhatsApp only.
_PARSERS: list[tuple[Callable[[str, list[str]], bool], Callable[[str, str], _Chat]]] = [
    (_detect_whatsapp, _parse_whatsapp),
]

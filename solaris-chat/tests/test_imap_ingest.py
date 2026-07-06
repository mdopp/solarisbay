"""IMAP email ingest adapter (#654, docs/okf-write-contract.md §3).

imaplib is mocked — a `FakeImap` returns canned RFC822 bytes for scripted UIDs
(patched over `imaplib.IMAP4_SSL`), so these cover the mail→event mapping, the
plain/HTML/charset/broken-Date body paths, the `<uidvalidity>:<last_uid>` cursor
advance + UIDVALIDITY reset, the `UID n:*` last-mail filter, the idempotent skip,
the read-only select, and per-mail failure isolation — no real IMAP server.

Schema is inlined DDL mirroring the #446 migration (importing alembic from a
solaris-chat test fails CI's clean env).
"""

from __future__ import annotations

import sqlite3

import pytest

import solaris_chat.engine.ingest.imap as imap_mod
from solaris_chat.config import ImapAccount
from solaris_chat.engine.ingest import ImapIngest
from solaris_chat.engine.knowledge import projection
from solaris_chat.engine.knowledge.writer import OkfWriter


# Mirrors database/migrations/versions/20260615_0016_okf_knowledge_index.py.
_SCHEMA = """
CREATE TABLE entities (
  id TEXT PRIMARY KEY, type TEXT NOT NULL, canonical_name TEXT NOT NULL,
  resident_uid TEXT NOT NULL, source TEXT NOT NULL, content_hash TEXT NOT NULL,
  updated TEXT NOT NULL DEFAULT (datetime('now')));
CREATE TABLE entity_aliases (
  entity_id TEXT NOT NULL, alias TEXT NOT NULL,
  PRIMARY KEY (entity_id, alias),
  FOREIGN KEY (entity_id) REFERENCES entities (id));
CREATE INDEX entity_aliases_alias_idx ON entity_aliases (alias);
CREATE TABLE facts (
  id TEXT PRIMARY KEY, subject_entity_id TEXT, resident_uid TEXT NOT NULL,
  predicate TEXT NOT NULL, value TEXT NOT NULL, confidence REAL,
  source TEXT NOT NULL, timestamp TEXT NOT NULL DEFAULT (datetime('now')),
  FOREIGN KEY (subject_entity_id) REFERENCES entities (id));
CREATE INDEX facts_subject_predicate_idx ON facts (subject_entity_id, predicate);
CREATE TABLE events (
  id TEXT PRIMARY KEY, ts TEXT NOT NULL, resident_uid TEXT NOT NULL,
  kind TEXT NOT NULL, source TEXT NOT NULL);
CREATE INDEX events_ts_idx ON events (ts);
CREATE INDEX events_resident_ts_idx ON events (resident_uid, ts);
CREATE TABLE event_entities (
  event_id TEXT NOT NULL, entity_id TEXT NOT NULL, role TEXT NOT NULL,
  PRIMARY KEY (event_id, entity_id, role),
  FOREIGN KEY (event_id) REFERENCES events (id),
  FOREIGN KEY (entity_id) REFERENCES entities (id));
CREATE TABLE concepts (
  id TEXT PRIMARY KEY, ref_id TEXT NOT NULL,
  ref_kind TEXT NOT NULL CHECK (ref_kind IN ('entity', 'event')),
  okf_path TEXT NOT NULL, embedding_id TEXT, content_hash TEXT NOT NULL,
  updated TEXT NOT NULL DEFAULT (datetime('now')));
CREATE TABLE ingest_log (
  source TEXT NOT NULL, external_id TEXT NOT NULL, content_hash TEXT NOT NULL,
  ingested_at TEXT NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (source, external_id));
CREATE INDEX ingest_log_source_external_idx ON ingest_log (source, external_id);
"""


class FakeImap:
    """A scripted read-only IMAP4_SSL replacement.

    `mails` maps a UID (int) -> raw RFC822 bytes. `uid("search", ...)` honours
    the `n:*` last-mail quirk (an empty range still returns the last mail).
    A UID present in `fetch_fails` returns a non-tuple fetch payload.
    """

    def __init__(self, *, mails, uidvalidity=100, fetch_fails=frozenset()):
        self._mails = mails
        self._uidvalidity = uidvalidity
        self._fetch_fails = fetch_fails
        self.selected_readonly = None
        self.logged_out = False

    def login(self, username, password):
        return ("OK", [b"ok"])

    def select(self, folder, readonly=False):
        self.selected_readonly = readonly
        return ("OK", [str(len(self._mails)).encode()])

    def response(self, name):
        if name == "UIDVALIDITY":
            return ("OK", [str(self._uidvalidity).encode()])
        return ("OK", [None])

    def uid(self, command, *args):
        if command == "search":
            spec = args[-1]
            low = int(spec.split(":")[0])
            uids = sorted(self._mails)
            hit = [u for u in uids if u >= low]
            if not hit and uids:
                hit = [uids[-1]]  # `n:*` returns the last mail even when < n.
            return ("OK", [b" ".join(str(u).encode() for u in hit)])
        if command == "fetch":
            uid = int(args[0])
            if uid in self._fetch_fails:
                return ("OK", [None])
            raw = self._mails[uid]
            return ("OK", [(f"{uid} (RFC822 {{{len(raw)}}}".encode(), raw)])
        raise AssertionError(f"unexpected uid command {command}")

    def logout(self):
        self.logged_out = True
        return ("BYE", [b"bye"])


@pytest.fixture
def env(tmp_path):
    db_path = str(tmp_path / "solaris.db")
    notes_dir = str(tmp_path / "notes")
    conn = sqlite3.connect(db_path)
    conn.executescript(_SCHEMA)
    conn.commit()
    conn.close()
    writer = OkfWriter(db_path=db_path, notes_dir=notes_dir)
    return writer, db_path, tmp_path


def _patch(monkeypatch, client):
    monkeypatch.setattr(imap_mod.imaplib, "IMAP4_SSL", lambda host, port: client)


def _account(**kw) -> ImapAccount:
    base = dict(
        host="imap.example.org",
        port=993,
        username="mdopp@example.org",
        password="s3cret",
        folder="Solaris",
        resident_uid="mdopp",
    )
    base.update(kw)
    return ImapAccount(**base)


def _mail(
    *,
    subject="Rechnung Mai",
    frm="shop@example.org",
    date="Fri, 30 May 2026 19:00:00 +0200",
    body="Ihre Rechnung liegt bei.",
    content_type="text/plain; charset=utf-8",
    charset="utf-8",
) -> bytes:
    headers = [
        f"Subject: {subject}",
        f"From: {frm}",
        f"Content-Type: {content_type}",
        "MIME-Version: 1.0",
    ]
    if date is not None:
        headers.append(f"Date: {date}")
    return ("\r\n".join(headers) + "\r\n\r\n" + body).encode(charset)


# --- mail -> event -----------------------------------------------------------


def test_mail_maps_to_email_event(env, monkeypatch):
    writer, db_path, tmp_path = env
    client = FakeImap(mails={5: _mail()})
    _patch(monkeypatch, client)
    stats = ImapIngest(writer).run_account(_account(), "")
    assert stats.seen == 1 and stats.written == 1 and stats.skipped == 0
    assert stats.cursor == "100:5"
    assert client.selected_readonly is True  # read-only select (curation).
    conn = projection.open_conn(db_path)
    event = conn.execute("SELECT * FROM events").fetchone()
    assert event["kind"] == "email"
    assert event["ts"] == "2026-05-30T19:00:00+02:00"
    assert event["resident_uid"] == "mdopp"
    concept = conn.execute(
        "SELECT okf_path FROM concepts WHERE ref_kind = 'event'"
    ).fetchone()
    assert concept["okf_path"].startswith("users/mdopp/okf/events/2026-05-30-")
    conn.close()
    text = (tmp_path / "notes" / concept["okf_path"]).read_text()
    assert "kind: email" not in text  # kind is a projection field, not frontmatter.
    assert "from: shop@example.org" in text
    assert "subject: Rechnung Mai" in text
    assert "Ihre Rechnung liegt bei." in text  # verbatim body.


def test_umlaut_charset_body_decodes(env, monkeypatch):
    writer, db_path, tmp_path = env
    raw = _mail(
        subject="Grüße",
        body="Schöne Grüße vom Bäcker",
        content_type="text/plain; charset=iso-8859-1",
        charset="iso-8859-1",
    )
    client = FakeImap(mails={1: raw})
    _patch(monkeypatch, client)
    ImapIngest(writer).run_account(_account(), "")
    conn = projection.open_conn(db_path)
    path = conn.execute("SELECT okf_path FROM concepts").fetchone()["okf_path"]
    conn.close()
    text = (tmp_path / "notes" / path).read_text()
    assert "Schöne Grüße vom Bäcker" in text


def test_html_only_body_is_stripped_to_text(env, monkeypatch):
    writer, db_path, tmp_path = env
    raw = _mail(
        body="<html><body><p>Hallo &amp; willkommen</p></body></html>",
        content_type="text/html; charset=utf-8",
    )
    client = FakeImap(mails={2: raw})
    _patch(monkeypatch, client)
    ImapIngest(writer).run_account(_account(), "")
    conn = projection.open_conn(db_path)
    path = conn.execute("SELECT okf_path FROM concepts").fetchone()["okf_path"]
    conn.close()
    text = (tmp_path / "notes" / path).read_text()
    assert "Hallo & willkommen" in text
    assert "<p>" not in text and "&amp;" not in text


def test_broken_date_falls_back_to_empty_when(env, monkeypatch):
    writer, db_path, _ = env
    client = FakeImap(mails={3: _mail(date="not a date")})
    _patch(monkeypatch, client)
    stats = ImapIngest(writer).run_account(_account(), "")
    assert stats.written == 1
    conn = projection.open_conn(db_path)
    # A malformed Date leaves ts empty rather than raising (#651 ISO compare).
    assert conn.execute("SELECT ts FROM events").fetchone()["ts"] == ""
    conn.close()


def test_missing_subject_uses_placeholder(env, monkeypatch):
    writer, db_path, _ = env
    raw = b"From: x@y.z\r\nContent-Type: text/plain\r\n\r\nbody"
    client = FakeImap(mails={4: raw})
    _patch(monkeypatch, client)
    stats = ImapIngest(writer).run_account(_account(), "")
    assert stats.written == 1
    conn = projection.open_conn(db_path)
    path = conn.execute("SELECT okf_path FROM concepts").fetchone()["okf_path"]
    conn.close()
    assert "kein-betreff" in path


# --- cursor / incremental ----------------------------------------------------


def test_cursor_skips_already_seen(env, monkeypatch):
    writer, db_path, _ = env
    client = FakeImap(mails={5: _mail(subject="A"), 8: _mail(subject="B")})
    _patch(monkeypatch, client)
    # Cursor says we already have up to UID 5 in this uidvalidity.
    stats = ImapIngest(writer).run_account(_account(), "100:5")
    assert stats.seen == 1 and stats.written == 1  # only UID 8 is new.
    assert stats.cursor == "100:8"


def test_n_star_last_mail_is_filtered(env, monkeypatch):
    writer, _, _ = env
    # Only UID 5 exists; cursor is already at 5. `UID 6:*` returns UID 5 (the
    # last mail) — the adapter must filter uid > last and ingest nothing.
    client = FakeImap(mails={5: _mail()})
    _patch(monkeypatch, client)
    stats = ImapIngest(writer).run_account(_account(), "100:5")
    assert stats.seen == 0 and stats.written == 0 and stats.cursor == "100:5"


def test_uidvalidity_change_rewalks_from_zero(env, monkeypatch):
    writer, db_path, _ = env
    # Server renumbered UIDs (new uidvalidity 200); the old high-water 999 is
    # meaningless — re-walk from 0. content_hash still dedups on a real re-run.
    client = FakeImap(mails={1: _mail()}, uidvalidity=200)
    _patch(monkeypatch, client)
    stats = ImapIngest(writer).run_account(_account(), "100:999")
    assert stats.seen == 1 and stats.written == 1
    assert stats.cursor == "200:1"


def test_reingest_unchanged_is_skipped(env, monkeypatch):
    writer, db_path, _ = env
    client = FakeImap(mails={5: _mail()})
    _patch(monkeypatch, client)
    ImapIngest(writer).run_account(_account(), "")
    # Re-run from a fresh (empty) cursor: UID 5 is walked again but the writer
    # short-circuits on the unchanged content_hash.
    stats = ImapIngest(writer).run_account(_account(), "")
    assert stats.seen == 1 and stats.written == 0 and stats.skipped == 1
    conn = projection.open_conn(db_path)
    assert projection.row_count(conn, "events") == 1
    conn.close()


# --- isolation ---------------------------------------------------------------


def test_fetch_failure_is_isolated_and_the_rest_ingest(env, monkeypatch):
    writer, db_path, _ = env
    client = FakeImap(
        mails={5: _mail(subject="bad"), 7: _mail(subject="good")},
        fetch_fails={5},
    )
    _patch(monkeypatch, client)
    stats = ImapIngest(writer).run_account(_account(), "")
    # UID 5's fetch returns nothing -> skipped; UID 7 still ingests; the cursor
    # advances past both so we never re-walk the bad one.
    assert stats.seen == 2 and stats.written == 1 and stats.skipped == 1
    assert stats.cursor == "100:7"
    conn = projection.open_conn(db_path)
    assert projection.row_count(conn, "events") == 1
    conn.close()


def test_logout_is_called(env, monkeypatch):
    writer, _, _ = env
    client = FakeImap(mails={1: _mail()})
    _patch(monkeypatch, client)
    ImapIngest(writer).run_account(_account(), "")
    assert client.logged_out is True

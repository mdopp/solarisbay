"""Messenger export drop-folder ingest (#655, docs/okf-write-contract.md §3.5).

A fake vault under `tmp_path` with WhatsApp export files (Android + iOS text,
umlauts, multiline, system line, `<Medien ausgeschlossen>`, unsaved-number
sender, zip variant) drives the scanner + WhatsApp parser end-to-end through the
real writer: per-day event grouping, participant `with` edges resolving to
person concepts, the file-level idempotent re-run, the 60 s mtime guard, and the
unrecognized-file-left-in-place path — no network.

Schema is inlined DDL mirroring the #446 migration (importing alembic from a
solaris-chat test fails CI's clean env).
"""

from __future__ import annotations

import io
import json
import os
import sqlite3
import time
import zipfile
from datetime import datetime, timezone

import pytest

from solaris_chat.engine.ingest import ExportsIngest
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


_ANDROID = "\n".join(
    [
        "15.05.24, 20:14 - Nachrichten sind Ende-zu-Ende-verschlüsselt.",
        "15.05.24, 20:15 - Anna: Hallo, wie geht's? Grüße!",
        "15.05.24, 20:16 - Bob: Gut, danke.",
        "Zweite Zeile der Nachricht.",
        "15.05.24, 20:17 - Anna: <Medien ausgeschlossen>",
        "16.05.24, 09:00 - Bob: Guten Morgen.",
    ]
)

_IOS = "\n".join(
    [
        "[15.05.24, 20:15:03] Anna: Hallo vom iPhone",
        "[15.05.24, 20:16:00] +49 171 1234567: Wer ist das?",
    ]
)


@pytest.fixture
def env(tmp_path):
    db_path = str(tmp_path / "solaris.db")
    notes_dir = tmp_path / "notes"
    conn = sqlite3.connect(db_path)
    conn.executescript(_SCHEMA)
    conn.commit()
    conn.close()
    writer = OkfWriter(db_path=db_path, notes_dir=str(notes_dir))
    ingest = ExportsIngest(writer, db_path=db_path, notes_dir=str(notes_dir))
    return ingest, db_path, notes_dir


def _drop(notes_dir, relpath: str, data, *, aged=True):
    path = notes_dir / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, str):
        path.write_text(data, encoding="utf-8")
    else:
        path.write_bytes(data)
    if aged:  # push mtime past the 60 s guard.
        old = time.time() - 120
        os.utime(path, (old, old))
    return path


def _events(db_path):
    conn = projection.open_conn(db_path)
    try:
        return conn.execute("SELECT * FROM events ORDER BY ts").fetchall()
    finally:
        conn.close()


# --- android + grouping -------------------------------------------------------


def test_android_export_groups_one_event_per_day(env):
    ingest, db_path, notes_dir = env
    _drop(notes_dir, "users/anna/inbox/exports/WhatsApp Chat mit Bob.txt", _ANDROID)
    stats = ingest.run()
    assert stats.events_written == 2  # 15th + 16th.
    rows = _events(db_path)
    assert [r["ts"] for r in rows] == ["2024-05-15T00:00:00", "2024-05-16T00:00:00"]
    assert all(r["kind"] == "chat" for r in rows)
    assert all(r["resident_uid"] == "anna" for r in rows)


def test_person_concepts_and_with_edges_resolve(env):
    ingest, db_path, notes_dir = env
    _drop(notes_dir, "users/anna/inbox/exports/WhatsApp Chat mit Bob.txt", _ANDROID)
    stats = ingest.run()
    assert stats.people_written == 2  # Anna + Bob.
    conn = projection.open_conn(db_path)
    try:
        people = {r["canonical_name"] for r in conn.execute("SELECT * FROM entities")}
        assert people == {"Anna", "Bob"}
        # The 15th's event has Anna + Bob as `with` participants (edges resolved).
        day15 = conn.execute(
            "SELECT id FROM events WHERE ts = ?", ("2024-05-15T00:00:00",)
        ).fetchone()["id"]
        roles = conn.execute(
            "SELECT role FROM event_entities WHERE event_id = ?", (day15,)
        ).fetchall()
        assert len(roles) == 2 and all(r["role"] == "with" for r in roles)
    finally:
        conn.close()


def test_body_umlauts_multiline_and_media_skip(env):
    ingest, db_path, notes_dir = env
    _drop(notes_dir, "users/anna/inbox/exports/WhatsApp Chat mit Bob.txt", _ANDROID)
    ingest.run()
    conn = projection.open_conn(db_path)
    try:
        path = conn.execute(
            "SELECT okf_path FROM concepts WHERE ref_kind = 'event' AND okf_path LIKE ?",
            ("%2024-05-15%",),
        ).fetchone()["okf_path"]
    finally:
        conn.close()
    text = (notes_dir / path).read_text(encoding="utf-8")
    assert "20:15 Anna: Hallo, wie geht's? Grüße!" in text
    assert "Gut, danke.\nZweite Zeile der Nachricht." in text  # continuation joined.
    assert "Medien ausgeschlossen" not in text  # media message skipped.
    assert "Ende-zu-Ende" not in text  # system line skipped.
    assert path.startswith("users/anna/okf/events/2024-05-15-")


# --- ios + unsaved number -----------------------------------------------------


def test_ios_bracket_format_and_unsaved_number_sender(env):
    ingest, db_path, notes_dir = env
    _drop(notes_dir, "users/anna/inbox/exports/WhatsApp Chat mit X.txt", _IOS)
    stats = ingest.run()
    assert stats.events_written == 1 and stats.people_written == 2
    conn = projection.open_conn(db_path)
    try:
        # An unsaved-number sender survives slugging (+49 171 ... -> digits/dashes).
        aliases = {r["canonical_name"] for r in conn.execute("SELECT * FROM entities")}
        assert "Anna" in aliases and "+49 171 1234567" in aliases
        # both participants land as `with` edges (slug of the number resolved).
        roles = conn.execute("SELECT COUNT(*) c FROM event_entities").fetchone()["c"]
        assert roles == 2
    finally:
        conn.close()


# --- zip variant --------------------------------------------------------------


def test_zip_export_reads_inner_txt(env):
    ingest, db_path, notes_dir = env
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("_chat.txt", _ANDROID)
        zf.writestr("IMG-001.jpg", b"\xff\xd8media")  # media member ignored.
    _drop(notes_dir, "inbox/exports/WhatsApp Bob.zip", buf.getvalue())
    stats = ingest.run()
    assert stats.events_written == 2
    rows = _events(db_path)
    assert all(r["resident_uid"] == "household" for r in rows)  # shared drop.


# --- idempotency / guards -----------------------------------------------------


def test_processed_move_and_rerun_is_noop(env):
    ingest, db_path, notes_dir = env
    p = _drop(notes_dir, "users/anna/inbox/exports/WhatsApp Chat mit Bob.txt", _ANDROID)
    ingest.run()
    assert not p.exists()  # moved out of inbox.
    moved = p.parent / "processed" / p.name
    assert moved.exists()
    os.utime(moved, (time.time() - 120, time.time() - 120))
    # Re-drop an identical file: the file-level ingest_log hash skips it.
    _drop(notes_dir, "users/anna/inbox/exports/WhatsApp Chat mit Bob.txt", _ANDROID)
    stats = ingest.run()
    assert stats.events_written == 0 and stats.processed == 0 and stats.skipped == 1


def test_processed_subtree_is_skipped_by_scan(env):
    ingest, db_path, notes_dir = env
    # A file already sitting in processed/ must not be re-ingested.
    _drop(
        notes_dir,
        "users/anna/inbox/exports/processed/WhatsApp Chat mit Bob.txt",
        _ANDROID,
    )
    stats = ingest.run()
    assert stats.files == 0 and stats.events_written == 0


def test_fresh_file_is_skipped_by_mtime_guard(env):
    ingest, db_path, notes_dir = env
    _drop(
        notes_dir,
        "users/anna/inbox/exports/WhatsApp Chat mit Bob.txt",
        _ANDROID,
        aged=False,  # just-written, within the 60 s guard.
    )
    stats = ingest.run()
    assert stats.files == 0 and stats.events_written == 0


def test_unrecognized_file_is_left_in_place(env):
    ingest, db_path, notes_dir = env
    # US month-first export: intentionally not matched (day/month swap corrupts ts).
    p = _drop(
        notes_dir,
        "users/anna/inbox/exports/notes.txt",
        "Just some random text\nno date prefix here",
    )
    stats = ingest.run()
    assert stats.unrecognized == 1 and stats.events_written == 0
    assert p.exists()  # left untouched for the human.


def test_two_residents_same_export_scoped_separately(env):
    ingest, db_path, notes_dir = env
    _drop(notes_dir, "users/anna/inbox/exports/WhatsApp Chat mit Bob.txt", _ANDROID)
    _drop(notes_dir, "users/dad/inbox/exports/WhatsApp Chat mit Bob.txt", _ANDROID)
    ingest.run()
    rows = _events(db_path)
    residents = {r["resident_uid"] for r in rows}
    assert residents == {"anna", "dad"}  # scoped copies, different paths.
    assert len(rows) == 4  # 2 days x 2 residents.


# --- signal-cli JSON parser ---------------------------------------------------

# Mid-day UTC epochs so local-time day-grouping is stable across runner TZs.
_T15_A = 1715774400000  # 2024-05-15 12:00:00 UTC
_T15_B = 1715778000000  # 2024-05-15 13:00:00 UTC
_T16 = 1715860800000  # 2024-05-16 12:00:00 UTC


def _local_day(ms: int) -> str:
    return (
        datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
        .astimezone()
        .strftime("%Y-%m-%d")
    )


def _sig_env(ts, name, message=None, *, has_data=True, group=None):
    data = None
    if has_data:
        data = {}
        if message is not None:
            data["message"] = message
        if group is not None:
            data["groupInfo"] = {"name": group}
    return {
        "envelope": {
            "timestamp": ts,
            "sourceName": name,
            "sourceNumber": "+49 171 1234567" if name is None else None,
            "dataMessage": data,
        }
    }


_SIGNAL_ARRAY = [
    _sig_env(_T15_A, "Anna", "Hallo, wie geht's? Grüße!"),
    _sig_env(_T15_B, "Bob", "Gut, danke."),
    _sig_env(_T15_B + 1000, "Anna", None),  # attachment-only (no message) — skipped.
    _sig_env(_T15_B + 2000, "Bob", has_data=False),  # receipt/typing — skipped.
    _sig_env(_T16, "Bob", "Guten Morgen."),
]


def test_signal_json_array_groups_one_event_per_day(env):
    ingest, db_path, notes_dir = env
    _drop(
        notes_dir,
        "users/anna/inbox/exports/signal-Bob.json",
        json.dumps(_SIGNAL_ARRAY),
    )
    stats = ingest.run()
    assert stats.events_written == 2  # 15th + 16th; skip-only envelopes dropped.
    rows = _events(db_path)
    assert [r["ts"] for r in rows] == [
        f"{_local_day(_T15_A)}T00:00:00",
        f"{_local_day(_T16)}T00:00:00",
    ]
    assert all(r["kind"] == "chat" for r in rows)
    assert all(r["resident_uid"] == "anna" for r in rows)


def test_signal_person_concepts_and_with_edges(env):
    ingest, db_path, notes_dir = env
    _drop(
        notes_dir,
        "users/anna/inbox/exports/signal-Bob.json",
        json.dumps(_SIGNAL_ARRAY),
    )
    stats = ingest.run()
    assert stats.people_written == 2  # Anna + Bob.
    conn = projection.open_conn(db_path)
    try:
        people = {r["canonical_name"] for r in conn.execute("SELECT * FROM entities")}
        assert people == {"Anna", "Bob"}
        day15 = conn.execute(
            "SELECT id FROM events WHERE ts = ?", (f"{_local_day(_T15_A)}T00:00:00",)
        ).fetchone()["id"]
        roles = conn.execute(
            "SELECT role FROM event_entities WHERE event_id = ?", (day15,)
        ).fetchall()
        assert len(roles) == 2 and all(r["role"] == "with" for r in roles)
    finally:
        conn.close()


def test_signal_body_title_and_skips(env):
    ingest, db_path, notes_dir = env
    _drop(
        notes_dir,
        "users/anna/inbox/exports/signal-Bob.json",
        json.dumps(_SIGNAL_ARRAY),
    )
    ingest.run()
    day15 = _local_day(_T15_A)
    conn = projection.open_conn(db_path)
    try:
        path = conn.execute(
            "SELECT okf_path FROM concepts WHERE ref_kind = 'event' AND okf_path LIKE ?",
            (f"%{day15}%",),
        ).fetchone()["okf_path"]
    finally:
        conn.close()
    text = (notes_dir / path).read_text(encoding="utf-8")
    assert "Anna: Hallo, wie geht's? Grüße!" in text  # umlauts preserved.
    assert "Bob: Gut, danke." in text
    assert f"Signal Bob {day15}" in text  # platform + chat name in the title.


def test_signal_ndjson_and_group_name(env):
    ingest, db_path, notes_dir = env
    ndjson = "\n".join(
        json.dumps(o)
        for o in [
            _sig_env(_T15_A, "Anna", "Hi crew", group="Family"),
            _sig_env(_T15_B, "Bob", "servus"),
        ]
    )
    _drop(notes_dir, "inbox/exports/signal_export.json", ndjson)
    stats = ingest.run()
    assert stats.events_written == 1
    conn = projection.open_conn(db_path)
    try:
        path = conn.execute(
            "SELECT okf_path FROM concepts WHERE ref_kind = 'event'"
        ).fetchone()["okf_path"]
    finally:
        conn.close()
    text = (notes_dir / path).read_text(encoding="utf-8")
    # The group name replaces the filename-derived chat name in the event title.
    assert "Signal Family" in text


def test_signal_unrecognized_json_left_in_place(env):
    ingest, db_path, notes_dir = env
    # A non-signal JSON (no `envelope`) must not be claimed by the signal parser.
    p = _drop(
        notes_dir,
        "users/anna/inbox/exports/other.json",
        json.dumps([{"foo": "bar"}]),
    )
    stats = ingest.run()
    assert stats.unrecognized == 1 and stats.events_written == 0
    assert p.exists()  # left untouched.


# --- SMS/RCS JSON parser (FOSS "SMS Import/Export") ---------------------------


def _sms(
    ms,
    *,
    address=None,
    type_=None,
    body=None,
    msg_box=None,
    parts=None,
    recipients=None,
):
    obj = {"date": str(ms)}
    if address is not None:
        obj["address"] = address
    if type_ is not None:
        obj["type"] = type_
    if body is not None:
        obj["body"] = body
    if msg_box is not None:
        obj["msg_box"] = msg_box
    if parts is not None:
        obj["parts"] = parts
    if recipients is not None:
        obj["recipients"] = recipients
    return obj


# SMS thread with the peer "+49 171 1234567": one received, one sent, on the 15th;
# a media-only MMS (no text — dropped) and a received SMS on the 16th.
_SMS_ARRAY = [
    _sms(_T15_A, address="+49 171 1234567", type_=1, body="Kommst du heute? Grüße"),
    _sms(_T15_B, address="+49 171 1234567", type_=2, body="Ja, um acht."),
    _sms(
        _T15_B + 1000,
        address="+49 171 1234567",
        msg_box=1,
        parts=[{"content_type": "image/jpeg"}],
    ),  # media-only — dropped.
    _sms(_T16, address="+49 171 1234567", type_=1, body="Guten Morgen."),
]


def test_sms_json_array_groups_one_event_per_day(env):
    ingest, db_path, notes_dir = env
    _drop(notes_dir, "users/anna/inbox/exports/sms-171.json", json.dumps(_SMS_ARRAY))
    stats = ingest.run()
    assert stats.events_written == 2  # 15th + 16th; media-only dropped.
    rows = _events(db_path)
    assert [r["ts"] for r in rows] == [
        f"{_local_day(_T15_A)}T00:00:00",
        f"{_local_day(_T16)}T00:00:00",
    ]
    assert all(r["kind"] == "chat" for r in rows)
    assert all(r["resident_uid"] == "anna" for r in rows)


def test_sms_person_concepts_resolve_by_number(env):
    ingest, db_path, notes_dir = env
    _drop(notes_dir, "users/anna/inbox/exports/sms-171.json", json.dumps(_SMS_ARRAY))
    stats = ingest.run()
    assert stats.people_written == 2  # the peer number + "Me".
    conn = projection.open_conn(db_path)
    try:
        people = {r["canonical_name"] for r in conn.execute("SELECT * FROM entities")}
        assert people == {"+49 171 1234567", "Me"}
        day15 = conn.execute(
            "SELECT id FROM events WHERE ts = ?", (f"{_local_day(_T15_A)}T00:00:00",)
        ).fetchone()["id"]
        roles = conn.execute(
            "SELECT role FROM event_entities WHERE event_id = ?", (day15,)
        ).fetchall()
        assert len(roles) == 2 and all(r["role"] == "with" for r in roles)
    finally:
        conn.close()


def test_sms_body_title_and_sent_marker(env):
    ingest, db_path, notes_dir = env
    _drop(notes_dir, "users/anna/inbox/exports/sms-171.json", json.dumps(_SMS_ARRAY))
    ingest.run()
    day15 = _local_day(_T15_A)
    conn = projection.open_conn(db_path)
    try:
        path = conn.execute(
            "SELECT okf_path FROM concepts WHERE ref_kind = 'event' AND okf_path LIKE ?",
            (f"%{day15}%",),
        ).fetchone()["okf_path"]
    finally:
        conn.close()
    text = (notes_dir / path).read_text(encoding="utf-8")
    assert "+49 171 1234567: Kommst du heute? Grüße" in text  # received, umlaut kept.
    assert "Me: Ja, um acht." in text  # sent side labelled "Me".
    # Single-peer thread names itself after the peer; platform prefix "SMS".
    assert f"SMS +49 171 1234567 {day15}" in text


def test_rcs_mms_parts_and_recipients_ndjson(env):
    ingest, db_path, notes_dir = env
    ndjson = "\n".join(
        json.dumps(o)
        for o in [
            # MMS/RCS: body in a text part, peer in recipients (self address skipped).
            _sms(
                _T15_A,
                msg_box=1,
                parts=[{"content_type": "text/plain", "text": "Foto vom See"}],
                recipients=[{"address": "+49 160 9998887", "type": 151}],
            ),
            _sms(
                _T15_B,
                msg_box=2,
                parts=[{"content_type": "text/plain", "text": "schön!"}],
                recipients=[
                    {"address": "+49 160 9998887", "type": 151},
                    {"address": "+49 171 0000000", "type": 137},
                ],
            ),
        ]
    )
    _drop(notes_dir, "users/anna/inbox/exports/messages_rcs.json", ndjson)
    stats = ingest.run()
    assert stats.events_written == 1
    conn = projection.open_conn(db_path)
    try:
        people = {r["canonical_name"] for r in conn.execute("SELECT * FROM entities")}
        path = conn.execute(
            "SELECT okf_path FROM concepts WHERE ref_kind = 'event'"
        ).fetchone()["okf_path"]
    finally:
        conn.close()
    # The 137-typed (self) recipient is not treated as the peer.
    assert people == {"+49 160 9998887", "Me"}
    text = (notes_dir / path).read_text(encoding="utf-8")
    assert "+49 160 9998887: Foto vom See" in text
    assert "Me: schön!" in text


def test_sms_does_not_claim_signal_json(env):
    ingest, db_path, notes_dir = env
    # A signal-cli export (has `envelope`, no `address`) stays with the signal
    # parser — the SMS detector must not swallow it.
    _drop(
        notes_dir,
        "users/anna/inbox/exports/signal-Bob.json",
        json.dumps(_SIGNAL_ARRAY),
    )
    ingest.run()
    conn = projection.open_conn(db_path)
    try:
        path = conn.execute(
            "SELECT okf_path FROM concepts WHERE ref_kind = 'event' LIMIT 1"
        ).fetchone()["okf_path"]
    finally:
        conn.close()
    text = (notes_dir / path).read_text(encoding="utf-8")
    assert "Signal Bob" in text  # signal parser owned it, not SMS.


def test_sms_unrecognized_json_left_in_place(env):
    ingest, db_path, notes_dir = env
    # JSON with neither `envelope` nor address/recipients/msg_box is unclaimed.
    p = _drop(
        notes_dir,
        "users/anna/inbox/exports/config.json",
        json.dumps({"date": "1715774400000", "unrelated": True}),
    )
    stats = ingest.run()
    assert stats.unrecognized == 1 and stats.events_written == 0
    assert p.exists()  # left untouched.

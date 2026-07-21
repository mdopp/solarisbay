"""Interactive Takeout import flow (#869, P4a): upload → classify → plan card →
callback → durable job → progress → result → Posteingang.

Covers the P4a backend + card wiring:
  - `classify_archive` inspects a Takeout `.zip` manifest and counts each
    category (calendar/contacts/keep/music), with an LLM fallback for an
    ambiguous top-level folder (mocked, fail-open);
  - `build_plan_card` renders the findings + choices in the action-card schema
    with an "Importieren" primary + a cancel, threading the archive + categories
    through the confirm params;
  - the `import` job kind is registered in the runner and, dispatched over a
    stored archive, runs each selected category's importer, streams progress, and
    yields a result summary;
  - the run lands a Posteingang note so imported data surfaces for triage;
  - it is idempotent (a re-run of the same archive re-invokes the same
    idempotent importers, no double-count in the summary) and per-resident.

The heavy importers (DAV PUT / ytmusic resolution) and the LLM are mocked — the
per-datatype write paths have their own tests (#865-#868); here we test the
orchestration, card payload, dispatch, progress/result, and idempotency.
"""

from __future__ import annotations

import io
import json
import sqlite3
import time
import zipfile

import pytest

from solaris_chat.engine.importers import jobs as jobs_mod
from solaris_chat.engine.importers.google_takeout import orchestrator as o
from solaris_chat.engine.importers.jobs import JobRunner, registered_kind

_ICS = (
    "BEGIN:VCALENDAR\r\nVERSION:2.0\r\n"
    "BEGIN:VEVENT\r\nUID:e1@g\r\nSUMMARY:Zahnarzt\r\nDTSTART:20260101T100000Z\r\n"
    "END:VEVENT\r\n"
    "BEGIN:VEVENT\r\nUID:e2@g\r\nSUMMARY:Konzert\r\nDTSTART:20260202T200000Z\r\n"
    "END:VEVENT\r\nEND:VCALENDAR\r\n"
)
_VCF = (
    "BEGIN:VCARD\r\nVERSION:3.0\r\nFN:Anna Beispiel\r\nUID:c1\r\nEND:VCARD\r\n"
    "BEGIN:VCARD\r\nVERSION:3.0\r\nFN:Bert Muster\r\nUID:c2\r\nEND:VCARD\r\n"
)
_KEEP = json.dumps({"title": "Einkaufen", "textContent": "Milch", "labels": []})
_HIST = json.dumps(
    [
        {
            "header": "YouTube Music",
            "title": "Anti-Hero angesehen",
            "titleUrl": "https://music.youtube.com/watch?v=vidA",
            "subtitles": [{"name": "Taylor Swift - Topic"}],
        }
    ]
)
# The sibling SEARCH history a German export ships next to the watch history —
# same list-of-records shape but `results?search_query=` URLs (no `watch?v=`), so
# the content-based finder must NOT mistake it for the watch history.
_SEARCH = json.dumps(
    [
        {
            "header": "YouTube",
            "title": "taylor swift gesucht",
            "titleUrl": "https://www.youtube.com/results?search_query=taylor+swift",
        }
    ]
)


def _make_zip(
    *, calendar=True, contacts=True, keep=True, music=True, extra=None
) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        if calendar:
            z.writestr("Takeout/Kalender/Privat.ics", _ICS)
        if contacts:
            z.writestr("Takeout/Kontakte/contacts.vcf", _VCF)
        if keep:
            z.writestr("Takeout/Notizen/note-1.json", _KEEP)
        if music:
            z.writestr(
                "Takeout/YouTube and YouTube Music/history/watch-history.json", _HIST
            )
        for name, body in (extra or {}).items():
            z.writestr(name, body)
    return buf.getvalue()


# ---- classify → plan card ---------------------------------------------------


def test_classify_counts_each_category():
    c = o.classify_archive(_make_zip())
    counts = {claim["category"]: claim["count"] for claim in c["claims"]}
    assert counts == {"calendar": 2, "contacts": 2, "keep": 1, "music": 1}
    assert len(c["hash"]) == 16


def test_classify_omits_empty_categories():
    c = o.classify_archive(_make_zip(contacts=False, music=False))
    assert {claim["category"] for claim in c["claims"]} == {"calendar", "keep"}


def test_classify_finds_localized_german_watch_history():
    # A German Takeout localises BOTH the folder and the FILENAME:
    # `Verlauf/Wiedergabeverlauf.json`, next to a `Suchverlauf.json` (search) and
    # `Playlists/*.csv`. The content-based finder must pick the watch history by
    # its `watch?v=` URLs — the exact case that regressed (issue #935).
    z = _make_zip(
        music=False,
        extra={
            "Takeout/YouTube und YouTube Music/Verlauf/Wiedergabeverlauf.json": _HIST,
            "Takeout/YouTube und YouTube Music/Verlauf/Suchverlauf.json": _SEARCH,
            "Takeout/YouTube und YouTube Music/Playlists/Zuhause-Videos.csv": "a,b\n1,2\n",
        },
    )
    counts = {
        claim["category"]: claim["count"] for claim in o.classify_archive(z)["claims"]
    }
    assert counts.get("music") == 1


def test_classify_search_history_alone_is_not_music():
    # The search history must NOT be mistaken for the watch history (no `watch?v=`).
    z = _make_zip(
        calendar=False,
        contacts=False,
        keep=False,
        music=False,
        extra={"Takeout/YouTube und YouTube Music/Verlauf/Suchverlauf.json": _SEARCH},
    )
    assert not any(c["category"] == "music" for c in o.classify_archive(z)["claims"])


def test_classify_stable_hash_is_idempotency_key():
    z = _make_zip()
    assert o.classify_archive(z)["hash"] == o.classify_archive(z)["hash"]
    assert o.content_hash(z) == o.classify_archive(z)["hash"]


def test_classify_uses_llm_for_ambiguous_folder():
    # A folder the mechanical hints don't recognise, holding a .ics; the LLM
    # classifies it as calendar so its events are counted.
    z = _make_zip(
        calendar=False,
        contacts=False,
        keep=False,
        music=False,
        extra={"Takeout/Mystery/thing.ics": _ICS},
    )
    assert o.classify_archive(z)["claims"] == []  # no LLM → unknown, dropped
    llm = lambda folder: "calendar"  # noqa: E731 — one-line stub
    c = o.classify_archive(z, llm=llm)
    assert {claim["category"]: claim["count"] for claim in c["claims"]} == {
        "calendar": 2
    }


def test_classify_llm_failopen():
    z = _make_zip(
        calendar=False,
        contacts=False,
        keep=False,
        music=False,
        extra={"Takeout/Mystery/thing.ics": _ICS},
    )

    def boom(folder):
        raise RuntimeError("ollama down")

    assert o.classify_archive(z, llm=boom)["claims"] == []  # error → unknown


def test_plan_card_schema():
    c = o.classify_archive(_make_zip())
    card = o.build_plan_card(c, "users/mdopp/imports/takeout-abc.zip")
    assert card["kind"] == "action"
    labels = [b["label"] for b in card["buttons"]]
    assert labels == ["Importieren", "Abbrechen"]
    confirm = card["buttons"][0]
    assert confirm["action_id"] == o.CONFIRM_ACTION
    assert confirm["style"] == "primary"
    assert confirm["params"]["archive_id"] == "users/mdopp/imports/takeout-abc.zip"
    assert set(confirm["params"]["categories"]) == {
        "calendar",
        "contacts",
        "keep",
        "music",
    }
    assert confirm["params"]["hash"] == c["hash"]
    assert card["buttons"][1]["action_id"] == o.CANCEL_ACTION


# ---- job dispatch → progress → result → Posteingang -------------------------

_JOBS_SCHEMA = """
CREATE TABLE engine_import_jobs (
  id TEXT PRIMARY KEY, owner_uid TEXT NOT NULL, kind TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending', payload TEXT NOT NULL DEFAULT '{}',
  progress TEXT NOT NULL DEFAULT '{}', error TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at TEXT NOT NULL DEFAULT (datetime('now')));
"""


@pytest.fixture
def db(tmp_path):
    path = str(tmp_path / "solaris.db")
    conn = sqlite3.connect(path)
    conn.executescript(_JOBS_SCHEMA)
    conn.close()
    return path


@pytest.fixture
def mock_importers(monkeypatch):
    """Stub each per-category importer so the flow is tested without DAV/ytmusic/LLM.

    Records how many times each category ran + returns a fixed count, so a re-run
    can be asserted to re-invoke the same idempotent importer."""
    calls: dict[str, int] = {"calendar": 0, "contacts": 0, "keep": 0, "music": 0}

    def cal(zip_bytes, names, cfg):
        calls["calendar"] += 1
        return 2

    def con(zip_bytes, names, cfg):
        calls["contacts"] += 1
        return 2

    def keep(zip_bytes, names, cfg):
        calls["keep"] += 1
        return 1

    def music(zip_bytes, history, cfg):
        calls["music"] += 1
        return 3

    monkeypatch.setattr(o, "_run_calendar", cal)
    monkeypatch.setattr(o, "_run_contacts", con)
    monkeypatch.setattr(o, "_run_keep", keep)
    monkeypatch.setattr(o, "_run_music", music)
    monkeypatch.setitem(o._CATEGORY_RUNNERS, "calendar", ("Kalender …", cal))
    monkeypatch.setitem(o._CATEGORY_RUNNERS, "contacts", ("Kontakte …", con))
    monkeypatch.setitem(o._CATEGORY_RUNNERS, "keep", ("Notizen …", keep))
    monkeypatch.setitem(o._CATEGORY_RUNNERS, "music", ("Music …", music))
    return calls


def _payload(tmp_path, zip_bytes, categories, owner="mdopp"):
    archive = tmp_path / "takeout.zip"
    archive.write_bytes(zip_bytes)
    return {
        "owner_uid": owner,
        "notes_dir": str(tmp_path / "notes"),
        "archive_path": str(archive),
        "categories": categories,
        "hash": o.content_hash(zip_bytes),
        "db_path": str(tmp_path / "solaris.db"),
        "ollama_url": "http://x",
        "model": "m",
    }


def _run(db, payload, owner="mdopp"):
    r = JobRunner(db)
    jid = r.start(owner, "import", payload)
    for _ in range(400):
        snap = r.get(jid, owner)
        if snap and snap["status"] in {"done", "failed"}:
            return r, jid, snap
        time.sleep(0.01)
    raise AssertionError(f"job never finished: {r.get(jid, owner)}")


def test_import_kind_registered():
    assert registered_kind("import")
    assert "import" in jobs_mod._RUNNERS


def test_job_dispatch_runs_each_category_and_summarises(db, tmp_path, mock_importers):
    zb = _make_zip()
    _, _, snap = _run(
        db, _payload(tmp_path, zb, ["calendar", "contacts", "keep", "music"])
    )
    assert snap["status"] == "done"
    assert snap["result"]["per_category"] == {
        "calendar": 2,
        "contacts": 2,
        "keep": 1,
        "music": 3,
    }
    assert mock_importers == {"calendar": 1, "contacts": 1, "keep": 1, "music": 1}


def test_job_only_runs_selected_categories(db, tmp_path, mock_importers):
    zb = _make_zip()
    _, _, snap = _run(db, _payload(tmp_path, zb, ["calendar", "keep"]))
    assert set(snap["result"]["per_category"]) == {"calendar", "keep"}
    assert mock_importers == {"calendar": 1, "contacts": 0, "keep": 1, "music": 0}


def test_job_streams_progress(db, tmp_path, mock_importers):
    """`run_import` yields a per-category progress event before the final result
    (the runner persists each yield onto the row → the card update)."""
    zb = _make_zip(music=False)
    p = _payload(tmp_path, zb, ["calendar", "contacts", "keep"])
    events = list(o.run_import(p))
    stages = [e["stage"] for e in events]
    assert stages == ["calendar", "contacts", "keep", "done"]
    # pct climbs monotonically across the per-category steps, ending at 100.
    pcts = [e["pct"] for e in events]
    assert pcts[0] == 0 and pcts[-1] == 100 and pcts == sorted(pcts)
    assert events[-1]["result"]["per_category"] == {
        "calendar": 2,
        "contacts": 2,
        "keep": 1,
    }


def test_result_lands_in_posteingang(db, tmp_path, mock_importers):
    zb = _make_zip()
    _, _, snap = _run(db, _payload(tmp_path, zb, ["calendar", "contacts"]))
    note_rel = snap["result"]["posteingang"]
    note = tmp_path / "notes" / note_rel
    assert note.exists()
    text = note.read_text()
    # A dated facts/ note with no `consolidated:` stamp → the Posteingang inbox.
    assert "kind: import" in text
    assert "calendar: 2" in text and "contacts: 2" in text
    assert note.parent.name == "facts"
    assert note.name[:10].count("-") == 2  # YYYY-MM-DD prefix


def test_job_idempotent_rerun_no_double(db, tmp_path, mock_importers):
    """Re-running the same archive re-invokes the (idempotent) importers and
    reports the same counts — no doubling in the summary."""
    zb = _make_zip(contacts=False, music=False)
    p = _payload(tmp_path, zb, ["calendar", "keep"])
    _, _, snap1 = _run(db, p)
    _, _, snap2 = _run(db, p)
    assert snap1["result"]["per_category"] == snap2["result"]["per_category"]
    # Each run invokes each importer once (the importers themselves overwrite by
    # stable key — asserted in #865-#868); the summary never doubles.
    assert mock_importers == {"calendar": 2, "contacts": 0, "keep": 2, "music": 0}


def test_job_owner_scoped(db, tmp_path, mock_importers):
    zb = _make_zip(contacts=False, music=False)
    r, jid, snap = _run(
        db, _payload(tmp_path, zb, ["calendar"], owner="lena"), owner="lena"
    )
    assert snap["status"] == "done"
    assert r.get(jid, "mdopp") is None  # not mdopp's job
    assert r.latest_for("mdopp") is None


def test_music_routes_through_run_music_import(db, tmp_path, monkeypatch):
    """The music category dispatches to `run_music_import` (the #868 album-fact
    path) — not a reimplemented writer."""
    seen = {}

    def fake_run_music_import(history_bytes, paths, **kw):
        seen["owner"] = kw["owner_uid"]
        seen["bytes"] = history_bytes
        yield {"stage": "done", "result": {"albums_written": 4}}

    monkeypatch.setattr(
        "solaris_chat.engine.importers.google_takeout.importers.music.run_music_import",
        fake_run_music_import,
    )
    zb = _make_zip(calendar=False, contacts=False, keep=False)
    p = _payload(tmp_path, zb, ["music"])
    p.update({"music_dir": str(tmp_path / "m"), "data_dir": str(tmp_path / "d")})
    _, _, snap = _run(db, p)
    assert snap["result"]["per_category"] == {"music": 4}
    assert seen["owner"] == "mdopp"
    assert b"watch-history" not in seen["bytes"]  # it's the file *content*, not name

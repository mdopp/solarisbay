"""Obsidian ingest adapter (#448, docs/okf-write-contract.md §6).

The vault reader is mocked (`FakeObsidianReader` yields `VaultNote`s) for the
adapter-mapping cases, and the concrete `VaultObsidianReader` is exercised on a
real tmp vault for the parse + `okf/`-subtree-skip cases. Together they cover
note→concept, frontmatter carry-over, wikilink→relationship (and plain-link
left alone), body preservation and the idempotent re-ingest skip — and prove
the source vault is never written (originals untouched).

Schema is built from inlined DDL mirroring the #446 migration (importing alembic
from a solaris-chat test fails CI's clean env).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator

import pytest

from solaris_chat.engine.ingest import ObsidianIngest
from solaris_chat.engine.ingest.obsidian_reader import VaultNote, VaultObsidianReader
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


class FakeObsidianReader:
    """A mocked read-only vault yielding canned `VaultNote`s."""

    def __init__(self, notes: list[VaultNote]):
        self.notes = notes

    def iter_notes(self) -> Iterator[VaultNote]:
        yield from self.notes


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


def _note(**kw) -> VaultNote:
    base = dict(relpath="note.md", folder="", title="A Note", body="Body text.")
    base.update(kw)
    return VaultNote(**base)


def _run(reader, writer, db_path, *, uid="mdopp"):
    ingest = ObsidianIngest(reader, writer, db_path=db_path, ingesting_uid=uid)
    return ingest.run()


# --- note -> concept ---------------------------------------------------------


def test_note_maps_to_note_concept_with_source_and_preserved_body(env):
    writer, db_path, tmp_path = env
    reader = FakeObsidianReader([_note(relpath="ideas/garden.md", body="Plant beans.")])
    stats = _run(reader, writer, db_path)
    assert stats.notes == 1 and stats.written == 1
    conn = projection.open_conn(db_path)
    ent = conn.execute("SELECT * FROM entities").fetchone()
    assert ent["type"] == "note" and ent["source"] == "obsidian"
    assert ent["resident_uid"] == "mdopp"
    # Provenance `obsidian:<relpath>` is the (source, external_id) ingest_log key.
    log = conn.execute("SELECT source, external_id FROM ingest_log").fetchone()
    assert (log["source"], log["external_id"]) == ("obsidian", "ideas/garden.md")
    conn.close()
    text = (tmp_path / "notes" / "okf" / "notes" / "a-note.md").read_text()
    assert "type: note" in text and "source: obsidian" in text
    assert "Plant beans." in text  # body preserved verbatim


def test_type_inferred_from_folder_when_no_frontmatter_type(env):
    writer, db_path, tmp_path = env
    reader = FakeObsidianReader(
        [_note(relpath="people/anna.md", folder="people", title="Anna")]
    )
    _run(reader, writer, db_path)
    conn = projection.open_conn(db_path)
    assert conn.execute("SELECT type FROM entities").fetchone()["type"] == "person"
    conn.close()
    assert (tmp_path / "notes" / "okf" / "people" / "anna.md").is_file()


def test_frontmatter_type_title_tags_timestamp_carried_over(env):
    writer, db_path, tmp_path = env
    reader = FakeObsidianReader(
        [
            _note(
                relpath="x.md",
                folder="",
                note_type="place",
                title="Club X",
                tags=["nightlife", "muenchen"],
                timestamp="2026-05-01T00:00:00",
            )
        ]
    )
    _run(reader, writer, db_path)
    text = (tmp_path / "notes" / "okf" / "places" / "club-x.md").read_text()
    assert "type: place" in text and "title: Club X" in text
    assert "timestamp: 2026-05-01T00:00:00" in text
    assert "- nightlife" in text and "- muenchen" in text


# --- wikilink -> relationship ------------------------------------------------


def test_wikilink_to_known_concept_becomes_relationship(env):
    writer, db_path, tmp_path = env
    # Anna is ingested first (a known person concept); the second note's
    # [[Anna]] then resolves to a `related -> [[people/anna]]` edge.
    reader = FakeObsidianReader(
        [
            _note(relpath="people/anna.md", folder="people", title="Anna"),
            _note(
                relpath="diary.md",
                title="Diary",
                body="Met [[Anna]] today.",
                wikilinks=["Anna"],
            ),
        ]
    )
    _run(reader, writer, db_path)
    text = (tmp_path / "notes" / "okf" / "notes" / "diary.md").read_text()
    assert "## Relationships" in text
    assert "- related → [[people/anna]]" in text
    # Body link preserved too (the original text is never mutated).
    assert "Met [[Anna]] today." in text
    conn = projection.open_conn(db_path)
    fact = conn.execute(
        "SELECT predicate, value FROM facts WHERE predicate = 'related'"
    ).fetchone()
    assert fact["value"] == "people/anna"
    conn.close()


def test_wikilink_to_unknown_target_stays_plain_link_no_relationship(env):
    writer, db_path, tmp_path = env
    reader = FakeObsidianReader(
        [
            _note(
                relpath="diary.md",
                title="Diary",
                body="Met [[Nobody]] today.",
                wikilinks=["Nobody"],
            )
        ]
    )
    _run(reader, writer, db_path)
    text = (tmp_path / "notes" / "okf" / "notes" / "diary.md").read_text()
    assert "## Relationships" not in text  # unknown target -> no edge
    assert "Met [[Nobody]] today." in text  # left as a plain body link
    conn = projection.open_conn(db_path)
    assert projection.row_count(conn, "facts") == 0
    conn.close()


# --- idempotent --------------------------------------------------------------


def test_reingest_unchanged_note_is_skipped(env):
    writer, db_path, _ = env
    reader = FakeObsidianReader([_note(relpath="x.md", body="same")])
    _run(reader, writer, db_path)
    stats = _run(
        FakeObsidianReader([_note(relpath="x.md", body="same")]), writer, db_path
    )
    assert stats.notes == 1 and stats.skipped == 1 and stats.written == 0
    conn = projection.open_conn(db_path)
    assert projection.row_count(conn, "entities") == 1
    conn.close()


def test_changed_note_reingests_no_dup(env):
    writer, db_path, _ = env
    _run(FakeObsidianReader([_note(relpath="x.md", body="first")]), writer, db_path)
    stats = _run(
        FakeObsidianReader([_note(relpath="x.md", body="second")]), writer, db_path
    )
    assert stats.written == 1 and stats.skipped == 0
    conn = projection.open_conn(db_path)
    assert projection.row_count(conn, "entities") == 1  # updated in place, no dup
    conn.close()


# --- VaultObsidianReader (real tmp vault, read-only) -------------------------


def test_vault_reader_parses_frontmatter_body_and_wikilinks(tmp_path):
    vault = tmp_path / "vault"
    (vault / "people").mkdir(parents=True)
    (vault / "people" / "anna.md").write_text(
        "---\ntype: person\ntitle: Anna Müller\ntags: [friend, muenchen]\n"
        "timestamp: 2026-01-02T00:00:00\n---\n\n# Anna\n\nSee [[places/club-x]].\n",
        encoding="utf-8",
    )
    notes = list(VaultObsidianReader(str(vault)).iter_notes())
    assert len(notes) == 1
    note = notes[0]
    assert note.relpath == "people/anna.md" and note.folder == "people"
    assert note.note_type == "person" and note.title == "Anna Müller"
    assert note.tags == ["friend", "muenchen"]
    assert note.timestamp == "2026-01-02T00:00:00"
    assert note.wikilinks == ["places/club-x"]
    assert "See [[places/club-x]]." in note.body


def test_vault_reader_skips_okf_subtree_and_facts(tmp_path):
    vault = tmp_path / "vault"
    (vault / "okf" / "people").mkdir(parents=True)
    (vault / "facts").mkdir(parents=True)
    (vault / "okf" / "people" / "gen.md").write_text(
        "---\ntype: person\n---\n", "utf-8"
    )
    (vault / "facts" / "f.md").write_text("a fact", "utf-8")
    (vault / "hand.md").write_text("hand-written", "utf-8")
    paths = {n.relpath for n in VaultObsidianReader(str(vault)).iter_notes()}
    assert paths == {"hand.md"}  # own OKF output + fact-capture dir excluded


def test_vault_reader_does_not_write_the_source(tmp_path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "n.md").write_text("body", encoding="utf-8")
    before = {p: p.read_bytes() for p in vault.rglob("*")}
    list(VaultObsidianReader(str(vault)).iter_notes())
    after = {p: p.read_bytes() for p in vault.rglob("*")}
    assert before == after  # read-only on the source vault

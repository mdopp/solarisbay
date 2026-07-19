"""Music-wishlist enrichment in the Bibliothekar night run (#859).

The import tool (solaris-import-google) drops a `type: music-wishlist` note per
resident whose body is `### <Artist>` / `- **<Album>**` bullets. It only knows
the DIGITAL library, so the night run cross-references the OKF library and
annotates each album bullet in place with `owned_physical`/`wishlist`/`source`.

`owned_physical` is the one signal the OKF cleanly derives today (a matching
album is already in the library); `wishlist`/`source` are emitted empty (no OKF
schema carries them yet — a graceful no-op, not invented schema).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from solaris_chat.engine import crons


@dataclass
class _Settings:
    notes_dir: str
    solaris_db_path: str


# Migration 0016 subset (entities/facts/concepts) replayed locally — no alembic,
# mirrors tests/test_music_query.py._SCHEMA.
_SCHEMA = """
CREATE TABLE entities (
  id TEXT PRIMARY KEY, type TEXT NOT NULL, canonical_name TEXT NOT NULL,
  resident_uid TEXT NOT NULL, source TEXT NOT NULL, content_hash TEXT NOT NULL,
  updated TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE facts (
  id TEXT PRIMARY KEY, subject_entity_id TEXT, resident_uid TEXT NOT NULL,
  predicate TEXT NOT NULL, value TEXT NOT NULL, confidence REAL,
  source TEXT NOT NULL, timestamp TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE concepts (
  id TEXT PRIMARY KEY, ref_id TEXT NOT NULL, ref_kind TEXT NOT NULL,
  okf_path TEXT NOT NULL, embedding_id TEXT, content_hash TEXT NOT NULL,
  updated TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def _band(conn, ent_id, name, slug, owner):
    conn.execute(
        "INSERT INTO entities (id, type, canonical_name, resident_uid, source,"
        " content_hash) VALUES (?, 'band', ?, ?, 'jellyfin', 'h')",
        (ent_id, name, owner),
    )
    conn.execute(
        "INSERT INTO concepts (id, ref_id, ref_kind, okf_path, content_hash)"
        " VALUES (?, ?, 'entity', ?, 'h')",
        (f"c-{ent_id}", ent_id, f"okf/bands/{slug}.md"),
    )


def _song(conn, notes_root, ent_id, title, album, band_slug, owner):
    conn.execute(
        "INSERT INTO entities (id, type, canonical_name, resident_uid, source,"
        " content_hash) VALUES (?, 'song', ?, ?, 'jellyfin', 'h')",
        (ent_id, title, owner),
    )
    conn.execute(
        "INSERT INTO facts (id, subject_entity_id, resident_uid, predicate, value,"
        " source) VALUES (?, ?, ?, 'by', ?, 'jellyfin')",
        (f"f-{ent_id}", ent_id, owner, f"bands/{band_slug}"),
    )
    song_path = f"okf/songs/{ent_id}.md"
    conn.execute(
        "INSERT INTO concepts (id, ref_id, ref_kind, okf_path, content_hash)"
        " VALUES (?, ?, 'entity', ?, 'h')",
        (f"c-{ent_id}", ent_id, song_path),
    )
    # The album lives only in the OKF file frontmatter (the jellyfin adapter puts
    # it in `extra`, which the writer renders to frontmatter but not to `facts`).
    p = notes_root / song_path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        f"---\ntype: song\ntitle: {title}\nalbum: {album}\nresident: {owner}\n"
        f"source: jellyfin\n---\n",
        encoding="utf-8",
    )


def _env(tmp_path):
    db = str(tmp_path / "solaris.db")
    conn = sqlite3.connect(db)
    conn.executescript(_SCHEMA)
    notes = tmp_path / "notes"
    notes.mkdir()
    # A household library album (Queen — A Night at the Opera) and a private one
    # under `lena` that mdopp must NOT see (#576).
    _band(conn, "b-queen", "Queen", "queen", "household")
    _song(
        conn,
        notes,
        "s-bohemian",
        "Bohemian Rhapsody",
        "A Night at the Opera",
        "queen",
        "household",
    )
    _band(conn, "b-lena", "Adele", "adele", "lena")
    _song(conn, notes, "s-hello", "Hello", "25", "adele", "lena")
    conn.commit()
    conn.close()
    settings = _Settings(notes_dir=str(notes), solaris_db_path=db)
    return crons.CronRunner(
        db_path=db,
        deep=object(),
        skills_dir="",
        context_window=32768,
        ingest_settings=settings,
    ), notes


_NOTE = """---
type: music-wishlist
source: solaris-import-google
resident: mdopp
generated: 2026-07-19T02:00:00
tags: [musik, einkaufsliste, wishlist]
---
# Musik-Einkaufsliste
## Rock
### Queen
- **A Night at the Opera** — 42 Abspielungen
  - Bohemian Rhapsody (30)
### Pink Floyd
- **The Wall** — 12 Abspielungen
  - Comfortably Numb (12)
"""


def test_enriches_matched_and_unmatched_albums(tmp_path):
    runner, notes = _env(tmp_path)
    note = notes / "users" / "mdopp" / "Musik-Einkaufsliste.md"
    note.parent.mkdir(parents=True)
    note.write_text(_NOTE, encoding="utf-8")

    count = runner._enrich_music_wishlists(str(notes))

    assert count == 2
    out = note.read_text(encoding="utf-8")
    # The album already in the library is flagged owned_physical: true.
    assert "- **A Night at the Opera**" in out
    opera = out.split("- **A Night at the Opera**", 1)[1]
    assert "owned_physical: true" in opera.split("### Pink Floyd", 1)[0]
    # An album with no library match is flagged owned_physical: false.
    wall = out.split("- **The Wall**", 1)[1]
    assert "owned_physical: false" in wall
    # wishlist/source are graceful no-ops (present but empty — no OKF schema yet).
    assert "wishlist:" in out and "source:" in out


def test_enrichment_is_idempotent(tmp_path):
    runner, notes = _env(tmp_path)
    note = notes / "users" / "mdopp" / "Musik-Einkaufsliste.md"
    note.parent.mkdir(parents=True)
    note.write_text(_NOTE, encoding="utf-8")

    assert runner._enrich_music_wishlists(str(notes)) == 2
    first = note.read_text(encoding="utf-8")
    # A second night run re-touches nothing (marker sub-bullet already present).
    assert runner._enrich_music_wishlists(str(notes)) == 0
    assert note.read_text(encoding="utf-8") == first


def test_private_library_album_not_visible_cross_resident(tmp_path):
    # mdopp's wishlist must not match lena's private "25" album (#576 scope).
    runner, notes = _env(tmp_path)
    note = notes / "users" / "mdopp" / "Musik-Einkaufsliste.md"
    note.parent.mkdir(parents=True)
    note.write_text(
        "---\ntype: music-wishlist\nresident: mdopp\n---\n"
        "# Musik-Einkaufsliste\n## Pop\n### Adele\n- **25** — 5 Abspielungen\n",
        encoding="utf-8",
    )

    assert runner._enrich_music_wishlists(str(notes)) == 1
    out = note.read_text(encoding="utf-8")
    assert "owned_physical: false" in out


def test_non_wishlist_note_untouched(tmp_path):
    runner, notes = _env(tmp_path)
    other = notes / "idee.md"
    other.write_text(
        "# Idee\n### Queen\n- **A Night at the Opera**\n", encoding="utf-8"
    )

    assert runner._enrich_music_wishlists(str(notes)) == 0
    assert "owned_physical" not in other.read_text(encoding="utf-8")

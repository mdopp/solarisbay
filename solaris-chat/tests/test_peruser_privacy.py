"""Per-user privacy slice 1 (#576): uid-filtered retrieval, default-deny.

Asserts the access-control core on EVERY model-facing retrieval path — the
notes_search tool, the research tool (which fans out to notes), and the
projection structured reads. The invariant under test: a query returns only
items where `resident_uid IN (caller_uid, 'household')`. A personal item owned
by one resident never surfaces for another, and an unknown/voice caller
(`household`) sees only shared items — never anyone's personal data.

All production data is `household` today; these tests seed synthetic
per-resident-tagged items to prove the mechanism isolates them.
"""

from __future__ import annotations

import json
import sqlite3

from solaris_chat.engine.knowledge import projection
from solaris_chat.engine.tools.notes import build_notes_tools
from solaris_chat.engine.tools.research import build_research_tools


def _note(root, name: str, owner: str | None) -> None:
    root.mkdir(parents=True, exist_ok=True)
    fm = f"---\nadded_by: {owner}\n---\n" if owner is not None else ""
    (root / name).write_text(
        f"{fm}# {name}\n\nGeheimnis Wintergarten Notiz.\n", encoding="utf-8"
    )


def _vault(tmp_path):
    """A vault with one note per owner — cdopp, mdopp, household, unowned."""
    root = tmp_path / "notes"
    _note(root, "cdopp_secret.md", "cdopp")
    _note(root, "mdopp_secret.md", "mdopp")
    _note(root, "shared.md", "household")
    _note(root, "legacy.md", None)
    return str(root)


def _search_handler(vault, caller_uid: str):
    for tool in build_notes_tools(vault, lambda: caller_uid):
        if tool.name == "notes_search":
            return tool.handler
    raise AssertionError("notes_search tool not built")


async def _search_paths(vault, caller_uid: str) -> set[str]:
    handler = _search_handler(vault, caller_uid)
    hits = json.loads(await handler({"query": "Wintergarten"}))
    return {h["path"] for h in hits}


# ---- notes_search tool (model-facing grep) -----------------------------------


async def test_notes_search_mdopp_sees_own_and_shared_not_cdopp(tmp_path):
    paths = await _search_paths(_vault(tmp_path), "mdopp")
    assert paths == {"mdopp_secret.md", "shared.md", "legacy.md"}
    assert "cdopp_secret.md" not in paths


async def test_notes_search_cdopp_sees_own_and_shared_not_mdopp(tmp_path):
    paths = await _search_paths(_vault(tmp_path), "cdopp")
    assert paths == {"cdopp_secret.md", "shared.md", "legacy.md"}
    assert "mdopp_secret.md" not in paths


async def test_notes_search_household_sees_only_shared(tmp_path):
    # Voice / unknown caller resolves to `household` → shared pool only, never
    # any resident's personal note (default-deny against a cross-user leak).
    paths = await _search_paths(_vault(tmp_path), "household")
    assert paths == {"shared.md", "legacy.md"}
    assert "mdopp_secret.md" not in paths
    assert "cdopp_secret.md" not in paths


# ---- research tool (fans out to the filtered notes path) ---------------------


async def _research_note_refs(vault, caller_uid: str) -> set[str]:
    tools = build_research_tools(notes_dir=vault, uid_getter=lambda: caller_uid)
    research = next(t for t in tools if t.name == "research").handler
    out = json.loads(await research({"query": "Wintergarten"}))
    return {s["ref"] for s in out["sources"] if s["kind"] == "notes"}


async def test_research_notes_source_is_uid_filtered_mdopp(tmp_path):
    refs = await _research_note_refs(_vault(tmp_path), "mdopp")
    assert refs == {"mdopp_secret.md", "shared.md", "legacy.md"}
    assert "cdopp_secret.md" not in refs


async def test_research_notes_source_household_only_shared(tmp_path):
    refs = await _research_note_refs(_vault(tmp_path), "household")
    assert refs == {"shared.md", "legacy.md"}
    assert "mdopp_secret.md" not in refs
    assert "cdopp_secret.md" not in refs


# ---- projection structured reads ---------------------------------------------

_SCHEMA = """
CREATE TABLE entities (
  id TEXT PRIMARY KEY, type TEXT, canonical_name TEXT, resident_uid TEXT,
  source TEXT, content_hash TEXT, updated TEXT DEFAULT (datetime('now'))
);
CREATE TABLE facts (
  id TEXT PRIMARY KEY, subject_entity_id TEXT, resident_uid TEXT,
  predicate TEXT, value TEXT, confidence REAL, source TEXT
);
CREATE TABLE events (
  id TEXT PRIMARY KEY, ts TEXT, resident_uid TEXT, kind TEXT, source TEXT
);
CREATE TABLE event_entities (
  event_id TEXT, entity_id TEXT, role TEXT, PRIMARY KEY (event_id, entity_id)
);
"""


def _kdb(tmp_path) -> str:
    """One shared entity carrying facts/events under three owners."""
    path = str(tmp_path / "knowledge.db")
    conn = sqlite3.connect(path)
    conn.executescript(_SCHEMA)
    conn.execute(
        "INSERT INTO entities (id, type, canonical_name, resident_uid, source,"
        " content_hash) VALUES ('ent-x', 'project', 'Wintergarten', 'household',"
        " 'seed', 'h')"
    )
    for owner in ("mdopp", "cdopp", "household"):
        conn.execute(
            "INSERT INTO facts (id, subject_entity_id, resident_uid, predicate,"
            " value, source) VALUES (?, 'ent-x', ?, 'note', ?, 'seed')",
            (f"f-{owner}", owner, f"fact-{owner}"),
        )
        conn.execute(
            "INSERT INTO events (id, ts, resident_uid, kind, source)"
            " VALUES (?, '2026-01-01T00:00', ?, ?, 'seed')",
            (f"ev-{owner}", owner, f"evt-{owner}"),
        )
        conn.execute(
            "INSERT INTO event_entities (event_id, entity_id, role)"
            " VALUES (?, 'ent-x', 'subject')",
            (f"ev-{owner}",),
        )
    conn.commit()
    conn.close()
    return path


def test_projection_facts_uid_filtered(tmp_path):
    conn = projection.open_conn(_kdb(tmp_path))
    try:
        mdopp = {f["value"] for f in projection.entity_facts(conn, "ent-x", "mdopp")}
        assert mdopp == {"fact-mdopp", "fact-household"}
        assert "fact-cdopp" not in mdopp

        cdopp = {f["value"] for f in projection.entity_facts(conn, "ent-x", "cdopp")}
        assert cdopp == {"fact-cdopp", "fact-household"}
        assert "fact-mdopp" not in cdopp

        # Voice / unknown caller → household-only, never a resident's fact.
        shared = {
            f["value"] for f in projection.entity_facts(conn, "ent-x", "household")
        }
        assert shared == {"fact-household"}
    finally:
        conn.close()


def test_projection_events_uid_filtered(tmp_path):
    conn = projection.open_conn(_kdb(tmp_path))
    try:
        mdopp = {e["kind"] for e in projection.entity_events(conn, "ent-x", "mdopp")}
        assert mdopp == {"evt-mdopp", "evt-household"}
        assert "evt-cdopp" not in mdopp

        shared = {
            e["kind"] for e in projection.entity_events(conn, "ent-x", "household")
        }
        assert shared == {"evt-household"}
        assert "evt-mdopp" not in shared
        assert "evt-cdopp" not in shared
    finally:
        conn.close()

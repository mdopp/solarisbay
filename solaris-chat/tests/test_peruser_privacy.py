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


# ---- notes_read tool (per-path read must enforce the owner filter, #576) -----


def _tool_handler(vault, caller_uid: str, name: str):
    for tool in build_notes_tools(vault, lambda: caller_uid):
        if tool.name == name:
            return tool.handler
    raise AssertionError(f"{name} tool not built")


async def test_notes_read_denies_other_residents_note(tmp_path):
    # A path is not a capability: guessing/reusing cdopp's deterministic path
    # must NOT leak its body — deny indistinguishably from a missing path.
    vault = _vault(tmp_path)
    for caller in ("mdopp", "household", "unknown-voice-uid"):
        read = _tool_handler(vault, caller, "notes_read")
        result = json.loads(await read({"path": "cdopp_secret.md"}))
        assert result == {"error": "not found"}, caller


async def test_notes_read_allows_shared_and_own(tmp_path):
    vault = _vault(tmp_path)
    # A shared (household) note is readable by anyone.
    for caller in ("mdopp", "cdopp", "household"):
        read = _tool_handler(vault, caller, "notes_read")
        shared = json.loads(await read({"path": "shared.md"}))
        assert shared["path"] == "shared.md"
        assert "Geheimnis" in shared["content"]
    # The owner can read their own note.
    read_own = _tool_handler(vault, "cdopp", "notes_read")
    own = json.loads(await read_own({"path": "cdopp_secret.md"}))
    assert own["path"] == "cdopp_secret.md"
    assert "Geheimnis" in own["content"]


# ---- note_write tool (model-written notes must be owner-tagged, #576) ---------


async def test_note_write_stamps_caller_as_owner(tmp_path):
    root = tmp_path / "notes"
    write = _tool_handler(str(root), "mdopp", "note_write")
    out = json.loads(
        await write({"path": "neu.md", "content": "Geheimnis Wintergarten Notiz."})
    )
    from solaris_chat import notes_search

    # Routed under the caller's path AND stamped added_by (#576 slice 2).
    assert out["written"] == "users/mdopp/neu.md"
    rel = out["written"]
    text = (root / rel).read_text(encoding="utf-8")
    assert notes_search.owner_of(rel, text) == "mdopp"
    # And the body survives the frontmatter stamp.
    assert "Geheimnis Wintergarten Notiz." in text


# ---- path-based ownership (#576 slice 2): users/<uid>/ + resident: leak -------


def _raw(root, rel: str, body: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


async def test_path_scopes_note_without_frontmatter(tmp_path):
    # users/cdopp/secret.md has NO frontmatter — the PATH alone makes it cdopp's.
    root = tmp_path / "notes"
    _raw(root, "users/cdopp/secret.md", "# secret\n\nGeheimnis Wintergarten.\n")
    for caller, visible in (("cdopp", True), ("mdopp", False), ("household", False)):
        paths = await _search_paths(str(root), caller)
        assert ("users/cdopp/secret.md" in paths) is visible, caller
        read = _tool_handler(str(root), caller, "notes_read")
        result = json.loads(await read({"path": "users/cdopp/secret.md"}))
        if visible:
            assert "Geheimnis" in result["content"]
        else:
            assert result == {"error": "not found"}, caller


async def test_resident_frontmatter_leak_closed(tmp_path):
    # An OKF concept file carries `resident: cdopp` (NOT added_by) — previously
    # owner_of read only added_by, so it leaked via search. Now it's scoped.
    root = tmp_path / "notes"
    _raw(
        root,
        "okf/people/anna.md",
        "---\ntype: person\nresident: cdopp\n---\n\nGeheimnis Wintergarten.\n",
    )
    assert "okf/people/anna.md" not in await _search_paths(str(root), "mdopp")
    assert "okf/people/anna.md" in await _search_paths(str(root), "cdopp")
    # A shared OKF file (resident: household) stays visible to everyone.
    _raw(
        root,
        "okf/places/wg.md",
        "---\ntype: place\nresident: household\n---\n\nGeheimnis Wintergarten.\n",
    )
    assert "okf/places/wg.md" in await _search_paths(str(root), "mdopp")


async def test_note_write_routes_under_caller_path(tmp_path):
    root = tmp_path / "notes"
    write = _tool_handler(str(root), "cdopp", "note_write")
    out = json.loads(
        await write({"path": "idee.md", "content": "Geheimnis Wintergarten Notiz."})
    )
    assert out["written"] == "users/cdopp/idee.md"
    text = (root / "users" / "cdopp" / "idee.md").read_text(encoding="utf-8")
    from solaris_chat import notes_search

    assert notes_search.owner_of("users/cdopp/idee.md", text) == "cdopp"
    # mdopp can't see cdopp's freshly written private note.
    assert "users/cdopp/idee.md" not in await _search_paths(str(root), "mdopp")


async def test_household_note_write_stays_shared_root(tmp_path):
    root = tmp_path / "notes"
    write = _tool_handler(str(root), "household", "note_write")
    out = json.loads(await write({"path": "haus.md", "content": "Wintergarten."}))
    assert out["written"] == "haus.md"


async def test_fact_store_routes_under_caller_path(tmp_path):
    root = tmp_path / "notes"
    fact = _tool_handler(str(root), "cdopp", "fact_store")
    out = json.loads(await fact({"fact": "Lieblingsfarbe blau"}))
    assert out["stored"].startswith("users/cdopp/facts/")
    # Household facts stay in the shared facts dir.
    hfact = _tool_handler(str(root), "household", "fact_store")
    hout = json.loads(await hfact({"fact": "Mülltonne Dienstag"}))
    assert hout["stored"].startswith("facts/")


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

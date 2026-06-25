"""Ranked + lightly-fuzzy notes_search (#591), and its #576 scoping re-assert."""

from __future__ import annotations

import json

from solaris_chat.engine.tools.notes import build_notes_tools


def _note(path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _search_tool(notes_dir, uid):
    for tool in build_notes_tools(notes_dir, lambda: uid):
        if tool.name == "notes_search":
            return tool
    raise AssertionError("notes_search tool missing")


async def _search(notes_dir, uid, query):
    tool = _search_tool(notes_dir, uid)
    return json.loads(await tool.handler({"query": query}))


# ---- fuzzy + ranking ---------------------------------------------------------


async def test_typo_in_title_still_found(tmp_path):
    root = tmp_path / "notes"
    _note(root / "wg.md", "# Wintergarten\n\nPlanung fuer das Projekt.\n")
    # Old grep needed the literal substring; 'Wintergaten' (typo) found nothing.
    hits = await _search(str(root), "household", "Wintergaten")
    assert [h["path"] for h in hits] == ["wg.md"]
    assert hits[0]["title"] == "Wintergarten"


async def test_title_match_ranks_above_body_only(tmp_path):
    root = tmp_path / "notes"
    # A: query word in the TITLE. B: query word only in the body.
    _note(root / "a.md", "# Heizung Wartung\n\nAllgemeine Notizen.\n")
    _note(root / "b.md", "# Sonstiges\n\nDie Heizung wurde gewartet.\n")
    hits = await _search(str(root), "household", "Heizung")
    paths = [h["path"] for h in hits]
    assert paths.index("a.md") < paths.index("b.md")


async def test_exact_multi_term_returned_and_top(tmp_path):
    root = tmp_path / "notes"
    # Matches BOTH terms in the body (today's exact-AND case) — must rank top.
    _note(root / "both.md", "# Garten\n\nDer Wintergarten und das Dach sind fertig.\n")
    # Only one term.
    _note(root / "one.md", "# Dach\n\nNur ueber das Dach.\n")
    hits = await _search(str(root), "household", "Wintergarten Dach")
    assert hits[0]["path"] == "both.md"
    assert "both.md" in {h["path"] for h in hits}


async def test_more_than_eight_weak_matches_capped(tmp_path):
    root = tmp_path / "notes"
    for i in range(12):
        _note(root / f"n{i}.md", f"# Note {i}\n\nDie Heizung Nummer {i}.\n")
    hits = await _search(str(root), "household", "Heizung")
    assert len(hits) == 8


async def test_no_term_match_excluded_below_floor(tmp_path):
    root = tmp_path / "notes"
    _note(root / "match.md", "# Wintergarten\n\nProjekt.\n")
    _note(root / "nomatch.md", "# Kochrezept\n\nNudeln mit Tomaten.\n")
    hits = await _search(str(root), "household", "Wintergarten")
    paths = {h["path"] for h in hits}
    assert "match.md" in paths
    assert "nomatch.md" not in paths


async def test_hits_carry_score_and_snippet(tmp_path):
    root = tmp_path / "notes"
    _note(root / "wg.md", "# Wintergarten\n\nDie Glasflaeche ist gross.\n")
    hits = await _search(str(root), "household", "Glasflaeche")
    assert hits[0]["score"] > 0
    assert "Glasflaeche" in hits[0]["snippet"]


# ---- per-user scoping re-assert (#576, security-critical) --------------------


async def test_scoping_withholds_other_residents_note(tmp_path):
    root = tmp_path / "notes"
    # A cdopp-private note that would score VERY high for the query.
    _note(
        root / "users" / "cdopp" / "secret.md",
        "---\nadded_by: cdopp\n---\n\n# Wintergarten\n\nWintergarten Wintergarten.\n",
    )
    # A shared note.
    _note(root / "shared.md", "# Wintergarten\n\nGemeinsame Notiz.\n")
    # mdopp must NOT get cdopp's note even though it ranks highest.
    hits = await _search(str(root), "mdopp", "Wintergarten")
    paths = {h["path"] for h in hits}
    assert "users/cdopp/secret.md" not in paths
    assert "shared.md" in paths


async def test_scoping_unknown_caller_sees_household_only(tmp_path):
    root = tmp_path / "notes"
    _note(
        root / "users" / "cdopp" / "p.md",
        "---\nadded_by: cdopp\n---\n\n# Garten\n\nGarten privat.\n",
    )
    _note(root / "shared.md", "# Garten\n\nGemeinsamer Garten.\n")
    # Unknown caller -> household: only the shared note.
    hits = await _search(str(root), "household", "Garten")
    paths = {h["path"] for h in hits}
    assert paths == {"shared.md"}


async def test_scoping_owner_sees_own_private_note(tmp_path):
    root = tmp_path / "notes"
    _note(
        root / "users" / "cdopp" / "mine.md",
        "---\nadded_by: cdopp\n---\n\n# Garten\n\nMein Garten.\n",
    )
    hits = await _search(str(root), "cdopp", "Garten")
    assert "users/cdopp/mine.md" in {h["path"] for h in hits}

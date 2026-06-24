"""Tests for the research-synthesis tool (#574, slice 1).

The notes + web query paths are mocked; we assert research() fans out to both,
trust-ranks (notes above web), dedups, caps, and returns the cited JSON shape.
Also asserts the knowledge-question line is present in _TOOL_DISCIPLINE.
"""

from __future__ import annotations

import json

import pytest

from solaris_chat.engine.client import _TOOL_DISCIPLINE
from solaris_chat.engine.tools import research as research_mod


def _tool(monkeypatch, *, notes_raw: str, web_raw: str, calls: dict):
    async def fake_notes_search(args):
        calls["notes"] = args.get("query")
        return notes_raw

    async def fake_ddgs(query):
        calls["web"] = query
        return web_raw

    # Reuse the real notes Tool wiring, but swap its search handler.
    from solaris_chat.engine.tools.notes import Tool, build_notes_tools

    real_tools = build_notes_tools("/some/vault", lambda: "household")

    def fake_build_notes_tools(notes_dir, uid_getter):
        out = []
        for t in real_tools:
            if t.name == "notes_search":
                out.append(Tool(t.name, t.description, t.parameters, fake_notes_search))
            else:
                out.append(t)
        return out

    monkeypatch.setattr(research_mod, "build_notes_tools", fake_build_notes_tools)
    monkeypatch.setattr(research_mod, "_ddgs_search", fake_ddgs)

    tools = research_mod.build_research_tools(
        notes_dir="/some/vault", uid_getter=lambda: "household"
    )
    return next(t for t in tools if t.name == "research").handler


async def test_fans_out_trust_ranks_and_cites(monkeypatch):
    calls: dict = {}
    notes_raw = json.dumps(
        [{"path": "facts/glas.md", "snippet": "Glasdach im Wintergarten"}]
    )
    web_raw = json.dumps(
        {
            "results": [
                {
                    "title": "Wintergarten bauen",
                    "url": "https://example.com/wg",
                    "snippet": "Anleitung",
                }
            ]
        }
    )
    handler = _tool(monkeypatch, notes_raw=notes_raw, web_raw=web_raw, calls=calls)

    out = json.loads(await handler({"query": "Wintergarten Glasdach"}))

    # Fanned out to BOTH paths with the same query.
    assert calls["notes"] == "Wintergarten Glasdach"
    assert calls["web"] == "Wintergarten Glasdach"

    sources = out["sources"]
    assert [s["kind"] for s in sources] == ["notes", "web"]  # notes ranked above web
    assert sources[0]["rank"] < sources[1]["rank"]
    assert sources[0]["ref"] == "facts/glas.md"
    assert sources[1]["ref"] == "https://example.com/wg"
    # Cited JSON shape.
    assert set(sources[0]) == {"rank", "kind", "title", "ref", "snippet"}
    assert "zitiere" in out["note"]


async def test_dedups_by_ref(monkeypatch):
    calls: dict = {}
    dup = "https://example.com/x"
    web_raw = json.dumps(
        {
            "results": [
                {"title": "A", "url": dup, "snippet": "a"},
                {"title": "B", "url": dup, "snippet": "b"},
            ]
        }
    )
    handler = _tool(monkeypatch, notes_raw="[]", web_raw=web_raw, calls=calls)

    out = json.loads(await handler({"query": "x"}))
    refs = [s["ref"] for s in out["sources"]]
    assert refs == [dup]  # the second duplicate dropped


async def test_caps_total_sources(monkeypatch):
    calls: dict = {}
    web_raw = json.dumps(
        {
            "results": [
                {"title": f"T{i}", "url": f"https://example.com/{i}", "snippet": "s"}
                for i in range(20)
            ]
        }
    )
    handler = _tool(monkeypatch, notes_raw="[]", web_raw=web_raw, calls=calls)

    out = json.loads(await handler({"query": "x"}))
    assert len(out["sources"]) <= 8


async def test_empty_query_returns_cited_shape(monkeypatch):
    handler = _tool(monkeypatch, notes_raw="[]", web_raw="{}", calls={})
    out = json.loads(await handler({"query": "   "}))
    assert out["sources"] == []
    assert "zitiere" in out["note"]


def test_tool_discipline_has_knowledge_question_line():
    assert "research" in _TOOL_DISCIPLINE
    assert "Wissensfrage" in _TOOL_DISCIPLINE


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))

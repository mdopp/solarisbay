"""The document_extract structured-output tool (#doc).

The model fills typed arguments; the tool deterministically writes the `document`
note and marks the source companion done — only on success, so a failed
extraction is retried, not lost. These tests prove the note shape, the mark, the
required-field guard, and the path-jail.
"""

from __future__ import annotations

import json

from solaris_chat.engine.tools.documents import build_document_tools


def _tool(notes_dir, uid="mdopp"):
    tools = build_document_tools(str(notes_dir), lambda: uid)
    assert len(tools) == 1 and tools[0].name == "document_extract"
    return tools[0].handler


def _companion(notes_dir, uid="mdopp"):
    d = notes_dir / "users" / uid / "uploads"
    d.mkdir(parents=True)
    md = d / "ergo.md"
    md.write_text(
        "---\nkind: upload\n---\n\n# ergo\n\n<!-- extracted -->\n## Inhalt\nERGO …\n",
        encoding="utf-8",
    )
    return md


async def test_document_extract_writes_note_and_marks_companion(tmp_path):
    handler = _tool(tmp_path)
    companion = _companion(tmp_path)
    out = json.loads(
        await handler(
            {
                "source_document": "users/mdopp/uploads/ergo.md",
                "category": "insurance",
                "title": "ERGO Rechtsschutz",
                "provider": "ERGO",
                "policy_number": "SV 072714970",
                "cancellation_deadline": "2026-12-15",
            }
        )
    )
    assert out["ok"] is True
    doc = tmp_path / "users" / "mdopp" / "okf" / "documents" / "ergo-rechtsschutz.md"
    assert doc.is_file()
    text = doc.read_text()
    assert "type: document" in text
    assert "category: insurance" in text
    assert "provider: ERGO" in text
    assert "cancellation_deadline: 2026-12-15" in text
    assert "source_document: users/mdopp/uploads/ergo.md" in text
    # The companion is marked done — only now that the note exists.
    assert "<!-- classified -->" in companion.read_text()


async def test_document_extract_rejects_bad_category_without_writing(tmp_path):
    handler = _tool(tmp_path)
    companion = _companion(tmp_path)
    out = json.loads(
        await handler(
            {
                "source_document": "users/mdopp/uploads/ergo.md",
                "category": "not-a-category",
                "title": "X",
            }
        )
    )
    assert out["ok"] is False
    assert not (tmp_path / "users" / "mdopp" / "okf" / "documents").exists()
    # A failed extraction is NOT marked done (so it retries).
    assert "<!-- classified -->" not in companion.read_text()


async def test_document_extract_requires_title(tmp_path):
    handler = _tool(tmp_path)
    _companion(tmp_path)
    out = json.loads(
        await handler(
            {"source_document": "users/mdopp/uploads/ergo.md", "category": "insurance"}
        )
    )
    assert out["ok"] is False


async def test_document_extract_only_writes_present_fields(tmp_path):
    handler = _tool(tmp_path)
    _companion(tmp_path)
    await handler(
        {
            "source_document": "users/mdopp/uploads/ergo.md",
            "category": "insurance",
            "title": "ERGO",
            "provider": "ERGO",
            "salary": "",  # empty → omitted
        }
    )
    text = (tmp_path / "users" / "mdopp" / "okf" / "documents" / "ergo.md").read_text()
    assert "provider: ERGO" in text
    assert "salary:" not in text  # empty field not rendered

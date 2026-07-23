"""Tests for topic-filtered note retrieval + the topic-items endpoint (#244)."""

from __future__ import annotations

from solaris_chat import notes_search
from solaris_chat.server import build_app
from tests.test_server import _FakeEngine


def _note(path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _vault(tmp_path):
    """A small vault with topic-tagged + untagged + cross-resident notes."""
    root = tmp_path / "notes"
    # Frontmatter-list form (media-ingestion / daily-chronicle), mdopp's note.
    _note(
        root / "book_wintergarten.md",
        "---\ntype: book\ntags:\n  - solaris/ingested\n  - topic/projekt/wintergarten\n"
        "added_by: mdopp\n---\n\n# Wintergarten-Planung\n\nNotes.\n",
    )
    # Inline `#topic/...` form (dynamic-skills fact), mdopp's note.
    _note(
        root / "fact_glass.md",
        "# Glasdach\n\n#topic/projekt/wintergarten #type/fact\nadded_by: mdopp\n",
    )
    # lena's note on the same topic — must not surface for mdopp (D3).
    _note(
        root / "lena_idea.md",
        "---\ntags:\n  - topic/projekt/wintergarten\nadded_by: lena\n---\n\n# Idee\n",
    )
    # A note on a sibling slug whose prefix overlaps — must NOT match.
    _note(
        root / "other.md",
        "---\ntags:\n  - topic/projekt/wintergartendach\nadded_by: mdopp\n---\n\n# X\n",
    )
    # A child-topic note — `projekt/wintergarten/glas` must NOT match the parent.
    _note(
        root / "child.md",
        "---\ntags:\n  - topic/projekt/wintergarten/glas\nadded_by: mdopp\n---\n\n# C\n",
    )
    # An untagged note.
    _note(root / "loose.md", "# Loose\n\nNo topic here.\n")
    return str(root)


def test_matches_both_tag_forms_for_owner(tmp_path):
    vault = _vault(tmp_path)
    items = notes_search.notes_for_topic(vault, "projekt/wintergarten", "mdopp")
    paths = {i["path"] for i in items}
    assert paths == {"book_wintergarten.md", "fact_glass.md"}


def test_title_from_heading(tmp_path):
    vault = _vault(tmp_path)
    items = notes_search.notes_for_topic(vault, "projekt/wintergarten", "mdopp")
    titles = {i["path"]: i["title"] for i in items}
    assert titles["book_wintergarten.md"] == "Wintergarten-Planung"
    assert titles["fact_glass.md"] == "Glasdach"


def test_per_resident_isolation(tmp_path):
    vault = _vault(tmp_path)
    # lena sees only her own tagged note on the same topic, not mdopp's (D3).
    items = notes_search.notes_for_topic(vault, "projekt/wintergarten", "lena")
    assert {i["path"] for i in items} == {"lena_idea.md"}


def test_no_prefix_or_child_false_match(tmp_path):
    vault = _vault(tmp_path)
    paths = {
        i["path"]
        for i in notes_search.notes_for_topic(vault, "projekt/wintergarten", "mdopp")
    }
    assert "other.md" not in paths  # projekt/wintergartendach
    assert "child.md" not in paths  # projekt/wintergarten/glas


def test_unowned_note_is_shared(tmp_path):
    root = tmp_path / "notes"
    _note(
        root / "system.md",
        "---\ntags:\n  - topic/household\n---\n\n# Shared\n",
    )
    items = notes_search.notes_for_topic(str(root), "household", "anyone")
    assert {i["path"] for i in items} == {"system.md"}


def test_empty_when_vault_missing_or_no_match(tmp_path):
    assert notes_search.notes_for_topic(str(tmp_path / "nope"), "x", "mdopp") == []
    vault = _vault(tmp_path)
    assert notes_search.notes_for_topic(vault, "no/such/topic", "mdopp") == []
    assert notes_search.notes_for_topic(vault, "", "mdopp") == []


def test_is_upload_companion(tmp_path):
    # An upload companion at either location; a note elsewhere is not one (#998).
    assert notes_search.is_upload_companion("users/mdopp/uploads/scan.md")
    assert notes_search.is_upload_companion("uploads/scan.md")
    assert not notes_search.is_upload_companion("okf/notes/scan.md")
    assert not notes_search.is_upload_companion("users/mdopp/uploads/nested/x.md")


def test_walk_excludes_upload_companions(tmp_path):
    """The note walk skips upload companions — they're extraction scratch, not
    notes, so they can't collide with their derived OKF note (#998)."""
    root = tmp_path / "notes"
    _note(root / "users/mdopp/uploads/scan.md", "# Scan\n\nOCR text.\n")
    _note(root / "uploads/shared.md", "# Shared\n\nOCR text.\n")
    _note(root / "okf/notes/scan.md", "# Scan\n\nExtracted note.\n")
    _note(root / "loose.md", "# Loose\n\nA real note.\n")
    walked = {p.relative_to(root).as_posix() for p in notes_search.iter_vault_md(root)}
    assert walked == {"okf/notes/scan.md", "loose.md"}


# ---- Endpoint ----


async def test_topic_items_endpoint_filters_by_tag_and_resident(
    aiohttp_client, tmp_path
):
    vault = _vault(tmp_path)
    app = build_app(
        engine=_FakeEngine(),
        remote_user_header="Remote-User",
        default_uid="household",
        notes_dir=vault,
    )
    client = await aiohttp_client(app)
    resp = await client.get(
        "/api/topics/projekt/wintergarten/items",  # hierarchical slug in the path
        headers={"Remote-User": "mdopp"},
    )
    body = await resp.json()
    assert resp.status == 200
    assert body["ok"] is True
    assert body["slug"] == "projekt/wintergarten"
    assert {i["path"] for i in body["items"]} == {
        "book_wintergarten.md",
        "fact_glass.md",
    }


async def test_topic_items_endpoint_empty_for_unknown_topic(aiohttp_client, tmp_path):
    vault = _vault(tmp_path)
    app = build_app(
        engine=_FakeEngine(),
        remote_user_header="Remote-User",
        default_uid="household",
        notes_dir=vault,
    )
    client = await aiohttp_client(app)
    resp = await client.get(
        "/api/topics/no-such-topic/items", headers={"Remote-User": "mdopp"}
    )
    body = await resp.json()
    assert resp.status == 200
    assert body["items"] == []

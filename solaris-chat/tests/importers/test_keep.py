import io
import json
import zipfile

from solaris_chat.engine.importers.google_takeout.importers import keep

TEXT = json.dumps(
    {
        "title": "Einkauf",
        "textContent": "Milch\nBrot",
        "isPinned": True,
        "labels": [{"name": "Haushalt"}],
        "createdTimestampUsec": 1590000000000000,
        "userEditedTimestampUsec": 1600000000000000,
    }
).encode()
LIST = json.dumps(
    {
        "title": "Todo",
        "color": "RED",
        "listContent": [
            {"text": "A", "isChecked": False},
            {"text": "B", "isChecked": True},
        ],
        "createdTimestampUsec": 1591000000000000,
    }
).encode()
TRASH = json.dumps(
    {
        "title": "Weg",
        "textContent": "x",
        "isTrashed": True,
        "createdTimestampUsec": 1592000000000000,
    }
).encode()


def _target(tmp_path, user):
    return tmp_path / "notes" / "users" / user / "Google Keep"


def test_preview_skips_trashed():
    p = keep.preview([("a.json", TEXT), ("b.json", LIST), ("c.json", TRASH)])
    assert p["notes"] == 2
    assert p["trashed_skipped"] == 1


def test_import_markdown_frontmatter_and_checkboxes(tmp_path):
    target = _target(tmp_path, "keepu1")
    rep = keep.do_import(
        target, [("a.json", TEXT), ("b.json", LIST), ("c.json", TRASH)]
    )
    assert rep["written"] == 2
    texts = [p.read_text() for p in target.glob("*.md")]
    joined = "\n".join(texts)
    assert 'tags: ["Haushalt"]' in joined
    assert "- [ ] A" in joined and "- [x] B" in joined
    assert "pinned: true" in joined


def test_zip_expands_and_copies_attachment(tmp_path):
    buf = io.BytesIO()
    note = json.dumps(
        {
            "title": "Foto",
            "textContent": "hi",
            "createdTimestampUsec": 1593000000000000,
            "attachments": [{"filePath": "img.png", "mimetype": "image/png"}],
        }
    )
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("Takeout/Keep/foto.json", note)
        z.writestr("Takeout/Keep/img.png", b"PNGDATA")
    target = _target(tmp_path, "keepu2")
    rep = keep.do_import(target, [("keep.zip", buf.getvalue())])
    assert rep["written"] == 1
    assert rep["attachments_copied"] == 1
    assert (target / "attachments" / "img.png").read_bytes() == b"PNGDATA"
    assert "![[attachments/img.png]]" in "\n".join(
        p.read_text() for p in target.glob("*.md")
    )


def test_missing_attachment_reported(tmp_path):
    note = json.dumps(
        {
            "title": "NoImg",
            "textContent": "hi",
            "createdTimestampUsec": 1594000000000000,
            "attachments": [{"filePath": "gone.jpg"}],
        }
    ).encode()
    target = _target(tmp_path, "keepu3")
    rep = keep.do_import(target, [("n.json", note)])
    assert rep["attachments_missing"] == 1

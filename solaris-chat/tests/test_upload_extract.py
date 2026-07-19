"""Upload text extraction (#…): the companion `.md` gains the document's real
text so FTS indexes it and the Bibliothekar keywords it.

The subprocess tools (pdftotext/pdftoppm/tesseract) aren't in CI, so every test
monkeypatches `subprocess.run` (or `extract_text`) — we assert dispatch/wiring,
not the tools themselves.
"""

from __future__ import annotations

import subprocess
import types

from solaris_chat.engine.ingest import upload_extract
from solaris_chat.engine.ingest.upload_extract import (
    EXTRACTED_MARKER,
    extract_into_companion,
    extract_text,
    ingest_uploads,
)


def _fake_run(stdout: str, calls: list[list[str]]):
    def run(cmd, *args, **kwargs):
        calls.append(cmd)
        return types.SimpleNamespace(stdout=stdout, returncode=0)

    return run


_READABLE_DE = (
    "Sehr geehrte Frau Dopp, mit der Rechtsschutzversicherung ist der Beitrag "
    "für das Jahr fällig. Bei Fragen wenden Sie sich an uns. "
) * 3


def test_born_digital_pdf_uses_pdftotext_no_ocr(tmp_path, monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(subprocess, "run", _fake_run(_READABLE_DE, calls))
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF-1.4")

    text = extract_text(pdf)

    assert text == _READABLE_DE
    assert calls[0][0] == "pdftotext"
    assert all(c[0] != "pdftoppm" and c[0] != "tesseract" for c in calls)


def test_garbled_text_layer_falls_back_to_ocr(tmp_path, monkeypatch):
    # A broken font / rotated PDF: pdftotext returns long mojibake (clears the
    # length bar) with no real words — must route to OCR, not be kept as-is.
    calls: list[list[str]] = []
    (tmp_path / "page-01.png").write_bytes(b"img")

    def run(cmd, *args, **kwargs):
        calls.append(cmd)
        if cmd[0] == "pdftotext":
            return types.SimpleNamespace(
                stdout="ap obıa mmm yuog JIpaıyıun yeusoju J40pjassnd " * 20,
                returncode=0,
            )
        return types.SimpleNamespace(stdout="ERGO Versicherung", returncode=0)

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(
        upload_extract.tempfile, "TemporaryDirectory", lambda: _NoopTmp(str(tmp_path))
    )
    pdf = tmp_path / "scan.pdf"
    pdf.write_bytes(b"%PDF-1.4")

    text = extract_text(pdf)

    tools = [c[0] for c in calls]
    assert "pdftoppm" in tools and "tesseract" in tools
    assert "ERGO Versicherung" in text
    # OCR runs with orientation detection (--psm 1) for rotated scans.
    ocr = next(c for c in calls if c[0] == "tesseract")
    assert "--psm" in ocr and "1" in ocr


def test_is_readable_separates_prose_from_mojibake():
    assert upload_extract._is_readable(_READABLE_DE) is True
    assert upload_extract._is_readable("ap obıa mmm yuog JIpaıyıun " * 20) is False
    assert upload_extract._is_readable("nur drei kurze wörter") is False


def test_scanned_pdf_falls_back_to_ocr(tmp_path, monkeypatch):
    calls: list[list[str]] = []
    (tmp_path / "page-01.png").write_bytes(b"img")  # pdftoppm "output"

    def run(cmd, *args, **kwargs):
        calls.append(cmd)
        if cmd[0] == "pdftotext":
            return types.SimpleNamespace(stdout="", returncode=0)
        return types.SimpleNamespace(stdout="OCR TEXT", returncode=0)

    monkeypatch.setattr(subprocess, "run", run)
    # Rasterize into the same tmp dir the test seeded a fake page image in.
    monkeypatch.setattr(
        upload_extract.tempfile,
        "TemporaryDirectory",
        lambda: _NoopTmp(str(tmp_path)),
    )
    pdf = tmp_path / "scan.pdf"
    pdf.write_bytes(b"%PDF-1.4")

    text = extract_text(pdf)

    tools = [c[0] for c in calls]
    assert "pdftotext" in tools and "pdftoppm" in tools and "tesseract" in tools
    assert "OCR TEXT" in text


def test_image_calls_tesseract(tmp_path, monkeypatch):
    calls: list[list[str]] = []
    monkeypatch.setattr(subprocess, "run", _fake_run("BILDTEXT", calls))
    img = tmp_path / "photo.jpg"
    img.write_bytes(b"\xff\xd8\xff")

    text = extract_text(img)

    assert text == "BILDTEXT"
    assert calls[0][0] == "tesseract"
    assert "-l" in calls[0] and "deu+eng" in calls[0]


def test_unknown_type_returns_empty(tmp_path):
    other = tmp_path / "notes.txt"
    other.write_text("hi", encoding="utf-8")
    assert extract_text(other) == ""


def test_text_is_capped_with_marker(tmp_path, monkeypatch):
    monkeypatch.setattr(subprocess, "run", _fake_run("A" * 100_000, []))
    img = tmp_path / "big.png"
    img.write_bytes(b"img")
    text = extract_text(img)
    assert len(text) <= upload_extract._MAX_TEXT_CHARS + len("\n\n[… gekürzt …]")
    assert text.endswith("[… gekürzt …]")


def test_subprocess_failure_returns_empty(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise OSError("no such binary")

    monkeypatch.setattr(subprocess, "run", boom)
    pdf = tmp_path / "doc.pdf"
    pdf.write_bytes(b"%PDF")
    assert extract_text(pdf) == ""


def _companion(dir_path, stem="Police", media_ext=".pdf", body=None):
    dir_path.mkdir(parents=True, exist_ok=True)
    (dir_path / f"{stem}{media_ext}").write_bytes(b"raw")
    md = dir_path / f"{stem}.md"
    md.write_text(
        body
        if body is not None
        else f"---\nkind: upload\n---\n\n# {stem}\n\n![[{stem}{media_ext}]]\n",
        encoding="utf-8",
    )
    return md


def test_extract_into_companion_appends_and_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(upload_extract, "extract_text", lambda p: "POLICEN TEXT")
    md = _companion(tmp_path / "uploads")

    assert extract_into_companion(md) is True
    text = md.read_text(encoding="utf-8")
    assert "## Inhalt (extrahiert)" in text
    assert EXTRACTED_MARKER in text
    assert "POLICEN TEXT" in text

    # Second call is a no-op — the marker is already there.
    assert extract_into_companion(md) is False


def test_extract_into_companion_skips_already_extracted(tmp_path, monkeypatch):
    monkeypatch.setattr(upload_extract, "extract_text", lambda p: "SHOULD NOT RUN")
    md = _companion(
        tmp_path / "uploads",
        body=f"# X\n\n![[X.pdf]]\n\n{EXTRACTED_MARKER}\nalt\n",
    )
    assert extract_into_companion(md) is False


def test_extract_into_companion_no_media_returns_false(tmp_path, monkeypatch):
    monkeypatch.setattr(upload_extract, "extract_text", lambda p: "x")
    d = tmp_path / "uploads"
    d.mkdir(parents=True)
    md = d / "lonely.md"
    md.write_text("# lonely\n", encoding="utf-8")
    assert extract_into_companion(md) is False


def test_extract_into_companion_empty_extraction_returns_false(tmp_path, monkeypatch):
    monkeypatch.setattr(upload_extract, "extract_text", lambda p: "   ")
    md = _companion(tmp_path / "uploads")
    assert extract_into_companion(md) is False
    assert EXTRACTED_MARKER not in md.read_text(encoding="utf-8")


def test_ingest_uploads_counts_across_residents(tmp_path, monkeypatch):
    monkeypatch.setattr(upload_extract, "extract_text", lambda p: "TEXT")
    _companion(tmp_path / "users" / "anna" / "uploads", stem="A")
    _companion(tmp_path / "users" / "bob" / "uploads", stem="B")
    _companion(tmp_path / "uploads", stem="Shared")
    # An already-extracted one is not counted.
    _companion(
        tmp_path / "users" / "anna" / "uploads",
        stem="Done",
        body=f"# Done\n\n{EXTRACTED_MARKER}\nx\n",
    )

    assert ingest_uploads(str(tmp_path)) == 3


class _FakeEngine:
    async def dispatch_tool(self, name, arguments):  # pragma: no cover - unused
        return "{}"


def _app(tmp_path):
    from solaris_chat.server import build_app

    return build_app(
        engine=_FakeEngine(),
        remote_user_header="Remote-User",
        default_uid="household",
        solaris_db_path=str(tmp_path / "solaris.db"),
        notes_dir=str(tmp_path / "notes"),
    )


async def test_upload_download_serves_owned_file(aiohttp_client, tmp_path):
    up = tmp_path / "notes" / "users" / "anna" / "uploads"
    up.mkdir(parents=True)
    (up / "Police.pdf").write_bytes(b"%PDF-1.4 hello")
    client = await aiohttp_client(_app(tmp_path))
    r = await client.get("/api/uploads/Police.pdf", headers={"Remote-User": "anna"})
    assert r.status == 200
    assert await r.read() == b"%PDF-1.4 hello"


async def test_upload_download_rejects_traversal(aiohttp_client, tmp_path):
    (tmp_path / "notes").mkdir(parents=True)
    (tmp_path / "notes" / "secret.md").write_text("top secret\n", encoding="utf-8")
    client = await aiohttp_client(_app(tmp_path))
    r = await client.get(
        "/api/uploads/../../secret.md", headers={"Remote-User": "anna"}
    )
    assert r.status in (403, 404)


async def test_upload_download_requires_auth(aiohttp_client, tmp_path):
    (tmp_path / "notes").mkdir(parents=True)
    client = await aiohttp_client(_app(tmp_path))
    r = await client.get("/api/uploads/x.pdf")
    assert r.status == 401


class _NoopTmp:
    """A TemporaryDirectory stand-in that yields a fixed path and never deletes."""

    def __init__(self, path: str) -> None:
        self._path = path

    def __enter__(self) -> str:
        return self._path

    def __exit__(self, *exc) -> None:
        return None

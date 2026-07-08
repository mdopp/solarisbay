"""Notes-portal read-only aggregators for `#/p/notes` (#696).

Covers the three `/api/portal/notes*` endpoints against a temp vault: the
overview counts (notes/facts/inbox) + last-Bibliothekar trail + recent, the
browse groupings, and the single-note viewer with its path-jail and per-resident
scoping. A chat test must NOT import alembic (CI runs solaris-chat in a clean env
without it), so the vault is built as plain files on disk.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

from solaris_chat import notes_search
from solaris_chat.server import STATIC_DIR, build_app


class _FakeEngine:
    async def dispatch_tool(self, name, arguments):  # pragma: no cover - unused
        return "{}"


def _vault(tmp_path):
    """A small vault: a shared note, a private note per resident, a journal
    entry, an OKF concept + log, and two facts (one stale/unconsolidated → the
    inbox, one fresh → not counted)."""
    root = tmp_path / "notes"
    (root / "okf" / "people").mkdir(parents=True)
    (root / "facts").mkdir(parents=True)
    (root / "journal").mkdir(parents=True)
    (root / "users" / "anna").mkdir(parents=True)

    (root / "shared.md").write_text(
        "---\nadded_by: household\n---\n\n# Wintergarten\n#topic/projekt mit @anna\n",
        encoding="utf-8",
    )
    (root / "users" / "anna" / "geheim.md").write_text(
        "# Annas Notiz\nprivat\n", encoding="utf-8"
    )
    (root / "journal" / "2026-07-01.md").write_text(
        "# Tag\nheute war schön\n", encoding="utf-8"
    )
    (root / "okf" / "people" / "anna.md").write_text(
        "---\ntype: person\ndescription: Bewohnerin\n---\n\n# Anna\n", encoding="utf-8"
    )
    (root / "okf" / "log.md").write_text(
        "2026-07-05 merged x\n2026-07-06 stamped y\n", encoding="utf-8"
    )

    stale = (datetime.now(timezone.utc) - timedelta(days=10)).strftime("%Y-%m-%d")
    fresh = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    (root / "facts" / f"{stale}-alt.md").write_text(
        "# alt\nnoch offen\n", encoding="utf-8"
    )
    (root / "facts" / f"{fresh}-neu.md").write_text("# neu\nfrisch\n", encoding="utf-8")
    return root


def _app(tmp_path):
    return build_app(
        hermes=_FakeEngine(),
        remote_user_header="Remote-User",
        default_uid="household",
        solaris_db_path=str(tmp_path / "solaris.db"),
        notes_dir=str(_vault(tmp_path)),
    )


async def test_overview_counts_inbox_and_recent(aiohttp_client, tmp_path):
    client = await aiohttp_client(_app(tmp_path))
    j = await (
        await client.get("/api/portal/notes", headers={"Remote-User": "household"})
    ).json()
    assert j["ok"]
    # Inbox = the one stale, unconsolidated fact (the fresh one is excluded).
    assert j["counts"]["inbox"] == 1
    assert j["counts"]["facts"] == 2
    # The last Bibliothekar run is parsed from okf/log.md.
    assert j["librarian"][-1] == "2026-07-06 stamped y"
    # Recent lists modified notes with a title; anna's private note is NOT in the
    # household caller's recent (default-deny).
    assert all("geheim" not in r["path"] for r in j["recent"])


async def test_overview_scopes_to_caller(aiohttp_client, tmp_path):
    client = await aiohttp_client(_app(tmp_path))
    # Anna sees her own private note in recent; the household caller does not.
    j = await (
        await client.get("/api/portal/notes", headers={"Remote-User": "anna"})
    ).json()
    assert any("users/anna/geheim.md" == r["path"] for r in j["recent"])


async def test_browse_by_topic_and_okf(aiohttp_client, tmp_path):
    client = await aiohttp_client(_app(tmp_path))
    j = await (
        await client.get(
            "/api/portal/notes/browse?by=topic", headers={"Remote-User": "household"}
        )
    ).json()
    groups = {g["group"]: g["items"] for g in j["groups"]}
    assert "projekt" in groups
    assert groups["projekt"][0]["path"] == "shared.md"
    j = await (
        await client.get(
            "/api/portal/notes/browse?by=okf", headers={"Remote-User": "household"}
        )
    ).json()
    okf = {g["group"]: g["items"] for g in j["groups"]}
    assert "people" in okf and okf["people"][0]["path"] == "okf/people/anna.md"


async def test_browse_by_journal(aiohttp_client, tmp_path):
    client = await aiohttp_client(_app(tmp_path))
    j = await (
        await client.get(
            "/api/portal/notes/browse?by=journal", headers={"Remote-User": "household"}
        )
    ).json()
    paths = [it["path"] for g in j["groups"] for it in g["items"]]
    assert "journal/2026-07-01.md" in paths


async def test_note_viewer_returns_frontmatter_and_body(aiohttp_client, tmp_path):
    client = await aiohttp_client(_app(tmp_path))
    j = await (
        await client.get(
            "/api/portal/notes/note?path=okf/people/anna.md",
            headers={"Remote-User": "household"},
        )
    ).json()
    assert j["ok"]
    assert j["frontmatter"]["type"] == "person"
    assert j["frontmatter"]["description"] == "Bewohnerin"
    assert "# Anna" in j["content"]


async def test_note_viewer_path_jail_rejects_traversal(aiohttp_client, tmp_path):
    # A secret outside the vault must never be readable via `..`.
    (tmp_path / "secret.md").write_text("top secret\n", encoding="utf-8")
    client = await aiohttp_client(_app(tmp_path))
    r = await client.get(
        "/api/portal/notes/note?path=../secret.md",
        headers={"Remote-User": "household"},
    )
    assert r.status in (400, 404)


async def test_note_viewer_denies_other_resident(aiohttp_client, tmp_path):
    # The household caller may not read anna's private note.
    client = await aiohttp_client(_app(tmp_path))
    r = await client.get(
        "/api/portal/notes/note?path=users/anna/geheim.md",
        headers={"Remote-User": "household"},
    )
    assert r.status == 404
    # Anna herself may.
    r = await client.get(
        "/api/portal/notes/note?path=users/anna/geheim.md",
        headers={"Remote-User": "anna"},
    )
    assert r.status == 200


async def test_search_empty_query(aiohttp_client, tmp_path):
    client = await aiohttp_client(_app(tmp_path))
    j = await (
        await client.get(
            "/api/portal/notes/search?q=", headers={"Remote-User": "household"}
        )
    ).json()
    assert j == {"ok": True, "hits": []}


# --- #705: the real Syncthing vault (huge .stversions/, processed/, media) ---


def _syncthing_vault(tmp_path, versions=3000):
    """A vault that mirrors the real box: a handful of real notes plus a
    Syncthing `.stversions/` tree of thousands of historical `.md` copies, a
    `.stfolder` marker, a `processed/` inbox-export tree, and binary media. The
    pre-fix `rglob("*.md")` recursed the whole `.stversions/` tree → the overview
    scan never finished on the box; the fix prunes those subtrees."""
    root = _vault(tmp_path)
    (root / ".stfolder").mkdir()
    stv = root / ".stversions" / "journal"
    stv.mkdir(parents=True)
    for i in range(versions):
        (stv / f"2026-07-01~{i}.md").write_text(
            f"# ghost {i}\n#topic/projekt\nold copy\n", encoding="utf-8"
        )
    proc = root / "processed"
    proc.mkdir()
    for i in range(200):
        (proc / f"export-{i}.md").write_text(
            f"# consolidated {i}\n#topic/projekt\n", encoding="utf-8"
        )
    (root / "media").mkdir()
    (root / "media" / "photo.jpg").write_bytes(b"\xff\xd8\xff" * 4096)
    return root


def _big_app(tmp_path):
    return build_app(
        hermes=_FakeEngine(),
        remote_user_header="Remote-User",
        default_uid="household",
        solaris_db_path=str(tmp_path / "solaris.db"),
        notes_dir=str(_syncthing_vault(tmp_path)),
    )


def test_iter_vault_md_prunes_stversions_and_processed(tmp_path):
    # The load-bearing fix: the walk skips .stversions/.stfolder/processed and
    # media entirely — an unpruned rglob would return thousands of ghost copies.
    root = _syncthing_vault(tmp_path)
    rels = {str(p.relative_to(root)).replace("\\", "/") for p in iter_paths(root)}
    assert not any(r.startswith(".stversions/") for r in rels)
    assert not any(r.startswith("processed/") for r in rels)
    assert not any(r.endswith(".jpg") for r in rels)
    assert "shared.md" in rels
    # Only the ~6 real notes remain, not thousands.
    assert len(rels) < 20


def iter_paths(root):
    return list(notes_search.iter_vault_md(root))


async def test_overview_survives_the_real_vault(aiohttp_client, tmp_path):
    # Against the pre-fix rglob this scanned 3200+ ghost/export copies and hung on
    # the box; the pruned+off-loop scan answers fast and excludes .stversions.
    client = await aiohttp_client(_big_app(tmp_path))
    started = time.monotonic()
    j = await (
        await client.get("/api/portal/notes", headers={"Remote-User": "household"})
    ).json()
    elapsed = time.monotonic() - started
    assert j["ok"]
    assert elapsed < 5.0
    # The counts reflect only real notes, never the .stversions history copies.
    assert j["counts"]["notes"] < 20
    assert all(".stversions" not in r["path"] for r in j["recent"])
    assert all("processed/" not in r["path"] for r in j["recent"])


async def test_browse_excludes_stversions_and_processed(aiohttp_client, tmp_path):
    client = await aiohttp_client(_big_app(tmp_path))
    started = time.monotonic()
    j = await (
        await client.get(
            "/api/portal/notes/browse?by=topic", headers={"Remote-User": "household"}
        )
    ).json()
    elapsed = time.monotonic() - started
    assert j["ok"] and elapsed < 5.0
    paths = [it["path"] for g in j["groups"] for it in g["items"]]
    # #topic/projekt is stamped on the ghost + processed copies too — none surface.
    assert paths, "the real shared note should still group under projekt"
    assert all(".stversions" not in p for p in paths)
    assert all(not p.startswith("processed/") for p in paths)


# --- Frontend-contract checks (real check = box-verify) ---

_HTML = (STATIC_DIR / "index.html").read_text(encoding="utf-8")


def test_notes_route_and_nav_wired():
    # The router opens the notes portal, and the nav (rail + tabbar) carries it.
    assert 'type === "notes"' in _HTML or "renderNotesPage" in _HTML
    assert 'id="rail-notes"' in _HTML
    assert 'id="tab-notes"' in _HTML
    assert "#i-note" in _HTML

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

import pytest

from solaris_chat import notes_search, server
from solaris_chat.server import STATIC_DIR, build_app


@pytest.fixture(autouse=True)
def _clear_notes_caches():
    """The overview/stats TTL caches are module-level and keyed only by uid, so a
    prior test's payload would leak into a later same-uid one — reset between."""
    server._notes_overview_cache.clear()
    server._notes_stats_cache.clear()
    yield


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
        engine=_FakeEngine(),
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


async def test_browse_by_journal_dedups_same_day(aiohttp_client, tmp_path):
    # #709: the same day written under all three path conventions must show ONCE,
    # and the canonical `journal/<YYYY>/<date>.md` is the entry that survives.
    root = _vault(tmp_path)
    (root / "journal" / "2024").mkdir(parents=True, exist_ok=True)
    (root / "journal" / "2024-05-27.md").write_text("a\n", encoding="utf-8")
    (root / "journal" / "journal_2024-05-27.md").write_text("b\n", encoding="utf-8")
    (root / "journal" / "2024" / "2024-05-27.md").write_text("c\n", encoding="utf-8")
    app = build_app(
        engine=_FakeEngine(),
        remote_user_header="Remote-User",
        default_uid="household",
        solaris_db_path=str(tmp_path / "solaris.db"),
        notes_dir=str(root),
    )
    client = await aiohttp_client(app)
    j = await (
        await client.get(
            "/api/portal/notes/browse?by=journal", headers={"Remote-User": "household"}
        )
    ).json()
    paths = [it["path"] for g in j["groups"] for it in g["items"]]
    same_day = [p for p in paths if "2024-05-27" in p]
    assert same_day == ["journal/2024/2024-05-27.md"]


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
        engine=_FakeEngine(),
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


# --- Notizen V2: inbox curation workbench (#697) ---


class _FakeCrons:
    """Stands in for CronRunner.curate_scope — records the scope it was asked to
    curate so the endpoint's owner-scoping (a resident may only curate their own
    or the shared pool) can be asserted without a real librarian turn."""

    def __init__(self):
        self.scopes: list[str] = []

    async def curate_scope(self, notes_dir, scope):
        self.scopes.append(scope)
        return {"ok": True, "scope": scope, "candidates": 0, "summary": "nichts zu tun"}


def _v2_vault(tmp_path):
    """The V1 vault plus a private stale fact for anna and a Syncthing history
    tree, so the inbox scoping and the prune bound are both exercised."""
    root = _vault(tmp_path)
    (root / "users" / "anna" / "facts").mkdir(parents=True)
    stale = (datetime.now(timezone.utc) - timedelta(days=10)).strftime("%Y-%m-%d")
    (root / "users" / "anna" / "facts" / f"{stale}-annas-fakt.md").write_text(
        "---\nadded_by: anna\n---\n\n# annas fakt\nnur für anna\n", encoding="utf-8"
    )
    # A Syncthing history copy of a stale fact — pruned, must never inflate inbox.
    stv = root / ".stversions" / "facts"
    stv.mkdir(parents=True)
    for i in range(50):
        (stv / f"{stale}-ghost~{i}.md").write_text(
            "# ghost fakt\noffen\n", encoding="utf-8"
        )
    return root


def _v2_app(tmp_path, crons=None):
    return build_app(
        engine=_FakeEngine(),
        remote_user_header="Remote-User",
        default_uid="household",
        solaris_db_path=str(tmp_path / "solaris.db"),
        notes_dir=str(_v2_vault(tmp_path)),
        crons=crons,
    )


async def test_inbox_lists_stale_unconsolidated_only(aiohttp_client, tmp_path):
    client = await aiohttp_client(_v2_app(tmp_path))
    j = await (
        await client.get(
            "/api/portal/notes/inbox", headers={"Remote-User": "household"}
        )
    ).json()
    assert j["ok"]
    paths = [it["path"] for it in j["items"]]
    # The one shared stale, unconsolidated fact — never the fresh one, never a
    # .stversions ghost copy (the prune bound), never anna's private fact.
    assert any(p.endswith("-alt.md") and p.startswith("facts/") for p in paths)
    assert all("neu" not in p for p in paths)
    assert all(".stversions" not in p for p in paths)
    assert all("users/anna" not in p for p in paths)


async def test_inbox_scopes_to_caller(aiohttp_client, tmp_path):
    client = await aiohttp_client(_v2_app(tmp_path))
    j = await (
        await client.get("/api/portal/notes/inbox", headers={"Remote-User": "anna"})
    ).json()
    paths = [it["path"] for it in j["items"]]
    # Anna sees her own private stale fact plus the shared one.
    assert any("users/anna/facts/" in p for p in paths)
    assert any(p.startswith("facts/") for p in paths)


async def test_assign_folds_into_topic_and_stamps_source(aiohttp_client, tmp_path):
    root = _v2_vault(tmp_path)
    app = build_app(
        engine=_FakeEngine(),
        remote_user_header="Remote-User",
        default_uid="household",
        solaris_db_path=str(tmp_path / "solaris.db"),
        notes_dir=str(root),
    )
    client = await aiohttp_client(app)
    stale = (datetime.now(timezone.utc) - timedelta(days=10)).strftime("%Y-%m-%d")
    src_rel = f"facts/{stale}-alt.md"
    r = await client.post(
        "/api/portal/notes/assign",
        headers={"Remote-User": "household"},
        json={"path": src_rel, "target": "topic", "name": "garten"},
    )
    j = await r.json()
    assert j["ok"], j
    # The target topic note carries the fact body + a #topic anchor.
    tgt = root / j["target_path"]
    assert tgt.is_file()
    assert "#topic/garten" in tgt.read_text(encoding="utf-8")
    # Never-delete: the source still exists and is now stamped consolidated.
    src = root / src_rel
    assert src.is_file()
    assert "consolidated: true" in src.read_text(encoding="utf-8")
    # The move is logged to okf/log.md.
    assert "assign" in (root / "okf" / "log.md").read_text(encoding="utf-8")


async def test_assign_rejects_other_resident(aiohttp_client, tmp_path):
    root = _v2_vault(tmp_path)
    app = build_app(
        engine=_FakeEngine(),
        remote_user_header="Remote-User",
        default_uid="household",
        solaris_db_path=str(tmp_path / "solaris.db"),
        notes_dir=str(root),
    )
    client = await aiohttp_client(app)
    stale = (datetime.now(timezone.utc) - timedelta(days=10)).strftime("%Y-%m-%d")
    # The household caller may not touch anna's private fact.
    r = await client.post(
        "/api/portal/notes/assign",
        headers={"Remote-User": "household"},
        json={
            "path": f"users/anna/facts/{stale}-annas-fakt.md",
            "target": "topic",
            "name": "garten",
        },
    )
    assert r.status == 404


async def test_assign_path_jail_rejects_traversal(aiohttp_client, tmp_path):
    (tmp_path / "secret.md").write_text("top secret\n", encoding="utf-8")
    client = await aiohttp_client(_v2_app(tmp_path))
    r = await client.post(
        "/api/portal/notes/assign",
        headers={"Remote-User": "household"},
        json={"path": "../secret.md", "target": "topic", "name": "x"},
    )
    assert r.status in (400, 404)


async def test_archive_moves_never_deletes(aiohttp_client, tmp_path):
    root = _v2_vault(tmp_path)
    app = build_app(
        engine=_FakeEngine(),
        remote_user_header="Remote-User",
        default_uid="household",
        solaris_db_path=str(tmp_path / "solaris.db"),
        notes_dir=str(root),
    )
    client = await aiohttp_client(app)
    stale = (datetime.now(timezone.utc) - timedelta(days=10)).strftime("%Y-%m-%d")
    src_rel = f"facts/{stale}-alt.md"
    r = await client.post(
        "/api/portal/notes/archive",
        headers={"Remote-User": "household"},
        json={"path": src_rel},
    )
    j = await r.json()
    assert j["ok"], j
    # Never-delete: the fact is relocated under archive/, not gone.
    assert not (root / src_rel).is_file()
    assert (root / j["archived"]).is_file()
    assert j["archived"].startswith("archive/")
    assert "archive" in (root / "okf" / "log.md").read_text(encoding="utf-8")


async def test_curate_scopes_to_caller_and_runs_librarian(aiohttp_client, tmp_path):
    crons = _FakeCrons()
    client = await aiohttp_client(_v2_app(tmp_path, crons=crons))
    # A resident's curate is bounded to their own scope, never the whole vault.
    r = await client.post(
        "/api/portal/notes/curate",
        headers={"Remote-User": "anna"},
        json={"scope": "household"},
    )
    # An explicit shared scope is honoured (household pool).
    j = await r.json()
    assert j["ok"] and j["scope"] == "household"
    # A missing/foreign scope is coerced to the caller's own uid (default-deny).
    r = await client.post(
        "/api/portal/notes/curate",
        headers={"Remote-User": "anna"},
        json={"scope": "someone-else"},
    )
    j = await r.json()
    assert j["scope"] == "anna"
    assert crons.scopes == ["household", "anna"]


async def test_curate_without_librarian_is_503(aiohttp_client, tmp_path):
    client = await aiohttp_client(_v2_app(tmp_path))  # no crons wired
    r = await client.post(
        "/api/portal/notes/curate",
        headers={"Remote-User": "household"},
        json={},
    )
    assert r.status == 503


# --- Notizen V3: inline note editor (#698) ---


async def _note_via_get(client, path, user):
    return await (
        await client.get(
            "/api/portal/notes/note?path=" + path, headers={"Remote-User": user}
        )
    ).json()


async def test_note_get_returns_content_hash(aiohttp_client, tmp_path):
    client = await aiohttp_client(_app(tmp_path))
    j = await _note_via_get(client, "shared.md", "household")
    assert j["ok"] and j["hash"]


async def test_note_put_saves_verbatim(aiohttp_client, tmp_path):
    root = _vault(tmp_path)
    app = build_app(
        engine=_FakeEngine(),
        remote_user_header="Remote-User",
        default_uid="household",
        solaris_db_path=str(tmp_path / "solaris.db"),
        notes_dir=str(root),
    )
    client = await aiohttp_client(app)
    j = await _note_via_get(client, "shared.md", "household")
    new = "---\nadded_by: household\n---\n\n# Wintergarten neu\ngeändert\n"
    r = await client.put(
        "/api/portal/notes/note?path=shared.md",
        headers={"Remote-User": "household"},
        json={"content": new, "hash": j["hash"]},
    )
    body = await r.json()
    assert r.status == 200 and body["ok"], body
    # Frontmatter + body stored byte-for-byte; a fresh hash comes back.
    assert (root / "shared.md").read_text(encoding="utf-8") == new
    assert body["hash"] and body["hash"] != j["hash"]


async def test_note_put_stale_hash_is_409(aiohttp_client, tmp_path):
    root = _vault(tmp_path)
    app = build_app(
        engine=_FakeEngine(),
        remote_user_header="Remote-User",
        default_uid="household",
        solaris_db_path=str(tmp_path / "solaris.db"),
        notes_dir=str(root),
    )
    client = await aiohttp_client(app)
    j = await _note_via_get(client, "shared.md", "household")
    # A concurrent write moves the on-disk file past the caller's snapshot.
    (root / "shared.md").write_text("changed underneath\n", encoding="utf-8")
    r = await client.put(
        "/api/portal/notes/note?path=shared.md",
        headers={"Remote-User": "household"},
        json={"content": "mine\n", "hash": j["hash"]},
    )
    assert r.status == 409
    # No silent overwrite: the concurrent change survives.
    assert (root / "shared.md").read_text(encoding="utf-8") == "changed underneath\n"


async def test_note_put_path_jail_rejects_traversal(aiohttp_client, tmp_path):
    (tmp_path / "secret.md").write_text("top secret\n", encoding="utf-8")
    client = await aiohttp_client(_app(tmp_path))
    r = await client.put(
        "/api/portal/notes/note?path=../secret.md",
        headers={"Remote-User": "household"},
        json={"content": "pwned\n", "hash": "deadbeef"},
    )
    assert r.status in (400, 404)
    assert (tmp_path / "secret.md").read_text(encoding="utf-8") == "top secret\n"


async def test_note_put_denies_other_resident(aiohttp_client, tmp_path):
    root = _vault(tmp_path)
    app = build_app(
        engine=_FakeEngine(),
        remote_user_header="Remote-User",
        default_uid="household",
        solaris_db_path=str(tmp_path / "solaris.db"),
        notes_dir=str(root),
    )
    client = await aiohttp_client(app)
    # Anna may edit her own note; the household caller may not.
    j = await _note_via_get(client, "users/anna/geheim.md", "anna")
    assert j["ok"]
    r = await client.put(
        "/api/portal/notes/note?path=users/anna/geheim.md",
        headers={"Remote-User": "household"},
        json={"content": "overwrite\n", "hash": j["hash"]},
    )
    assert r.status == 404
    assert "privat" in (root / "users" / "anna" / "geheim.md").read_text(
        encoding="utf-8"
    )


# --- Notizen statistics (#699) ---


def _stats_vault(tmp_path):
    """The V1 vault plus tagged/linked/dated notes and a Syncthing history tree,
    so the top-N ranking, category breakdown, monthly growth, backlink counts and
    the prune bound are all exercised."""
    root = _vault(tmp_path)
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    (root / "topics").mkdir(exist_ok=True)
    # Three notes carry #topic/garten; one #urlaub — garten must rank first.
    for i in range(3):
        (root / "topics" / f"garten-{i}.md").write_text(
            f"---\ncreated: {month}\n---\n\n# g{i}\n#topic/garten mit @anna\n"
            "siehe [[Wintergarten]]\n",
            encoding="utf-8",
        )
    (root / "topics" / "reise.md").write_text(
        f"---\ncreated: {month}\n---\n\n# reise\n#urlaub [[Wintergarten]] [[Anna]]\n",
        encoding="utf-8",
    )
    # A Syncthing history copy — pruned, must never inflate any count.
    stv = root / ".stversions" / "topics"
    stv.mkdir(parents=True)
    for i in range(40):
        (stv / f"ghost~{i}.md").write_text(
            "# ghost\n#topic/garten @anna [[Wintergarten]]\n", encoding="utf-8"
        )
    return root


def _stats_app(tmp_path):
    return build_app(
        engine=_FakeEngine(),
        remote_user_header="Remote-User",
        default_uid="household",
        solaris_db_path=str(tmp_path / "solaris.db"),
        notes_dir=str(_stats_vault(tmp_path)),
    )


async def test_stats_top_tags_persons_and_categories(aiohttp_client, tmp_path):
    client = await aiohttp_client(_stats_app(tmp_path))
    j = await (
        await client.get(
            "/api/portal/notes/stats", headers={"Remote-User": "household"}
        )
    ).json()
    assert j["ok"]
    tags = {t["value"]: t["count"] for t in j["tags"]}
    # #topic/garten mentioned by exactly 3 real notes (ghost copies pruned).
    assert tags.get("topic/garten") == 3
    assert tags.get("urlaub") == 1
    # @anna counted once per note that mentions it.
    persons = {p["value"]: p["count"] for p in j["persons"]}
    assert persons.get("anna", 0) >= 3
    # Category breakdown groups the topics/ folder.
    cats = {c["value"]: c["count"] for c in j["categories"]}
    assert cats.get("topics") == 4


async def test_stats_growth_and_most_linked(aiohttp_client, tmp_path):
    client = await aiohttp_client(_stats_app(tmp_path))
    j = await (
        await client.get(
            "/api/portal/notes/stats", headers={"Remote-User": "household"}
        )
    ).json()
    # A dense 12-month series, this month non-zero.
    assert len(j["months"]) == 12
    key = datetime.now(timezone.utc).strftime("%Y-%m")
    assert any(m["month"] == key and m["count"] >= 4 for m in j["months"])
    # Most-linked: [[Wintergarten]] links from 4 notes (3 garten + reise).
    linked = {link_["value"]: link_["count"] for link_ in j["linked"]}
    assert linked.get("Wintergarten") == 4


async def test_stats_survives_and_prunes_stversions(aiohttp_client, tmp_path):
    # The ghost copies under .stversions/ must never inflate any count.
    client = await aiohttp_client(_stats_app(tmp_path))
    j = await (
        await client.get(
            "/api/portal/notes/stats", headers={"Remote-User": "household"}
        )
    ).json()
    tags = {t["value"]: t["count"] for t in j["tags"]}
    assert tags.get("topic/garten") == 3  # not 43


async def test_stats_scopes_to_caller(aiohttp_client, tmp_path):
    # Anna's private note carries #topic/projekt too; the household caller's
    # stats must exclude it (default-deny), anna's must include it.
    root = _stats_vault(tmp_path)
    (root / "users" / "anna" / "privat.md").write_text(
        "---\nadded_by: anna\n---\n\n# privat\n#nurAnna\n", encoding="utf-8"
    )
    app = build_app(
        engine=_FakeEngine(),
        remote_user_header="Remote-User",
        default_uid="household",
        solaris_db_path=str(tmp_path / "solaris.db"),
        notes_dir=str(root),
    )
    client = await aiohttp_client(app)
    hh = await (
        await client.get(
            "/api/portal/notes/stats", headers={"Remote-User": "household"}
        )
    ).json()
    an = await (
        await client.get("/api/portal/notes/stats", headers={"Remote-User": "anna"})
    ).json()
    assert all(t["value"] != "nuranna" for t in hh["tags"])
    assert any(t["value"] == "nuranna" for t in an["tags"])


# --- Frontend-contract checks (real check = box-verify) ---

_HTML = (STATIC_DIR / "index.html").read_text(encoding="utf-8")


def test_notes_v3_editor_ui_wired():
    # The inline editor (#698): the Edit toggle opens a source textarea that PUTs
    # the content + hash back.
    assert "openNoteEditor" in _HTML
    assert 'method: "PUT"' in _HTML
    assert "/api/portal/notes/note?path=" in _HTML
    assert "note-editor" in _HTML
    assert "Speichern" in _HTML and "Abbrechen" in _HTML


def test_notes_stats_ui_wired():
    # The Statistik section (#699): loader, ranked lists, and the growth chart.
    assert "loadNotesStats" in _HTML
    assert "/api/portal/notes/stats" in _HTML
    assert "Häufige Schlagwörter" in _HTML
    assert "growthChart" in _HTML
    assert "Meist verlinkt" in _HTML


def test_notes_route_and_nav_wired():
    # The router opens the notes portal, and the nav (rail + tabbar) carries it.
    assert 'type === "notes"' in _HTML or "renderNotesPage" in _HTML
    assert 'id="rail-notes"' in _HTML
    assert 'id="tab-notes"' in _HTML
    assert "#i-note" in _HTML


def test_notes_v2_inbox_ui_wired():
    # The inbox curation workbench (#697): the loader, per-entry actions, and the
    # curate trigger are all present in the notes page.
    assert "loadNotesInbox" in _HTML
    assert "/api/portal/notes/inbox" in _HTML
    assert "/api/portal/notes/curate" in _HTML
    # assign/archive are POSTed via the shared inboxAction helper ("/notes/" + kind).
    assert 'inboxAction(container, "assign"' in _HTML
    assert 'inboxAction(container, "archive"' in _HTML
    assert '"/api/portal/notes/" + kind' in _HTML
    assert "Jetzt kuratieren" in _HTML
    assert "→ Thema" in _HTML and "→ Person" in _HTML and "Archivieren" in _HTML


# --- DB-backed overview + stats (perf #830-follow-up): serve from solaris.db, no
#     full-vault walk. The projection schema is owned by alembic (not importable
#     from a chat test), so these seed the tables with the same DDL by hand.

import sqlite3  # noqa: E402

from solaris_chat import notes_index  # noqa: E402

_PROJECTION_DDL = """
CREATE TABLE entities (
  id TEXT PRIMARY KEY, type TEXT NOT NULL, canonical_name TEXT NOT NULL,
  resident_uid TEXT NOT NULL, source TEXT NOT NULL, content_hash TEXT NOT NULL,
  updated TEXT NOT NULL DEFAULT (datetime('now')));
CREATE TABLE entity_aliases (
  entity_id TEXT NOT NULL, alias TEXT NOT NULL, PRIMARY KEY (entity_id, alias));
CREATE TABLE facts (
  id TEXT PRIMARY KEY, subject_entity_id TEXT, resident_uid TEXT NOT NULL,
  predicate TEXT NOT NULL, value TEXT NOT NULL, confidence REAL,
  source TEXT NOT NULL, timestamp TEXT NOT NULL DEFAULT (datetime('now')));
CREATE TABLE events (
  id TEXT PRIMARY KEY, ts TEXT NOT NULL, resident_uid TEXT NOT NULL,
  kind TEXT NOT NULL, source TEXT NOT NULL);
CREATE TABLE event_entities (
  event_id TEXT NOT NULL, entity_id TEXT NOT NULL, role TEXT NOT NULL,
  PRIMARY KEY (event_id, entity_id, role));
CREATE TABLE concepts (
  id TEXT PRIMARY KEY, ref_id TEXT NOT NULL, ref_kind TEXT NOT NULL,
  okf_path TEXT NOT NULL, embedding_id TEXT, content_hash TEXT NOT NULL,
  updated TEXT NOT NULL DEFAULT (datetime('now')));
CREATE TABLE mentions (
  session_id TEXT NOT NULL, message_ref INTEGER NOT NULL, kind TEXT NOT NULL,
  value TEXT NOT NULL, owner_uid TEXT NOT NULL,
  PRIMARY KEY (session_id, message_ref, kind, value));
"""


def _seed_projection(tmp_path):
    """A vault + a solaris.db projection over it: FTS rows for the notes, OKF
    concepts/entities/events for recent/categories/growth/most-linked, and inline
    mentions for tags/persons — so the DB-backed path has real data to serve."""
    root = tmp_path / "notes"
    (root / "okf" / "people").mkdir(parents=True)
    (root / "okf" / "events").mkdir(parents=True)
    (root / "facts").mkdir(parents=True)
    (root / "users" / "anna").mkdir(parents=True)
    (root / "shared.md").write_text("# Wintergarten\n", encoding="utf-8")
    (root / "okf" / "people" / "anna.md").write_text(
        "---\ntype: person\n---\n\n# Anna\n", encoding="utf-8"
    )
    (root / "okf" / "events" / "2026-07-01-fest.md").write_text(
        "# Fest\n", encoding="utf-8"
    )
    (root / "facts" / "2026-07-01-alt.md").write_text("# alt\n", encoding="utf-8")
    (root / "users" / "anna" / "geheim.md").write_text("# geheim\n", encoding="utf-8")

    db = str(tmp_path / "solaris.db")
    # FTS index over the vault (fts_notes / fts_notes_meta) — the note-count source.
    notes_index.backfill(db, str(root))

    conn = sqlite3.connect(db)
    conn.executescript(_PROJECTION_DDL)
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    conn.executemany(
        "INSERT INTO entities"
        " (id, type, canonical_name, resident_uid, source, content_hash, updated)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            ("e-anna", "person", "Anna", "household", "okf", "h1", f"{month}-01 10:00"),
            ("e-fest", "event", "Fest", "household", "okf", "h2", f"{month}-01 11:00"),
        ],
    )
    conn.executemany(
        "INSERT INTO concepts"
        " (id, ref_id, ref_kind, okf_path, content_hash, updated)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        [
            (
                "c-anna",
                "e-anna",
                "entity",
                "okf/people/anna.md",
                "h1",
                f"{month}-02 09:00",
            ),
            (
                "c-fest",
                "e-fest",
                "event",
                "okf/events/2026-07-01-fest.md",
                "h2",
                f"{month}-05 09:00",
            ),
        ],
    )
    conn.execute(
        "INSERT INTO events (id, ts, resident_uid, kind, source)"
        " VALUES ('ev1', ?, 'household', 'gathering', 'okf')",
        (f"{month}-01T11:00:00",),
    )
    conn.execute(
        "INSERT INTO event_entities (event_id, entity_id, role)"
        " VALUES ('ev1', 'e-anna', 'participant')"
    )
    conn.executemany(
        "INSERT INTO mentions (session_id, message_ref, kind, value, owner_uid)"
        " VALUES (?, ?, ?, ?, ?)",
        [
            ("s1", 0, "tag", "garten", "household"),
            ("s1", 1, "tag", "garten", "household"),
            ("s2", 0, "tag", "garten", "household"),
            ("s1", 0, "tag", "urlaub", "household"),
            ("s1", 0, "person", "anna", "household"),
            ("s1", 0, "tag", "nuranna", "anna"),
        ],
    )
    conn.commit()
    conn.close()
    return root, db


def _db_app(tmp_path):
    root, db = _seed_projection(tmp_path)
    return build_app(
        engine=_FakeEngine(),
        remote_user_header="Remote-User",
        default_uid="household",
        solaris_db_path=db,
        notes_dir=str(root),
    )


async def test_overview_from_db_no_vault_walk(aiohttp_client, tmp_path, monkeypatch):
    # A full-vault walk must NOT run: fail the test loudly if iter_vault_md is hit.
    def _boom(*a, **k):
        raise AssertionError("full-vault walk ran on the DB-backed overview")

    app = _db_app(tmp_path)
    monkeypatch.setattr(notes_search, "iter_vault_md", _boom)
    client = await aiohttp_client(app)
    j = await (
        await client.get("/api/portal/notes", headers={"Remote-User": "household"})
    ).json()
    assert j["ok"]
    # 4 shared notes indexed (anna's private note excluded for the household caller).
    assert j["counts"]["notes"] == 4
    assert j["counts"]["facts"] == 1
    # recent comes from concepts.updated (newest first): the event's file leads.
    assert j["recent"][0]["path"] == "okf/events/2026-07-01-fest.md"
    assert {r["path"] for r in j["recent"]} == {
        "okf/events/2026-07-01-fest.md",
        "okf/people/anna.md",
    }


async def test_overview_from_db_scopes_to_caller(aiohttp_client, tmp_path):
    client = await aiohttp_client(_db_app(tmp_path))
    j = await (
        await client.get("/api/portal/notes", headers={"Remote-User": "anna"})
    ).json()
    # Anna sees her own private note in the count (5), household saw only 4.
    assert j["counts"]["notes"] == 5


async def test_stats_from_db_no_vault_walk(aiohttp_client, tmp_path, monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("full-vault walk ran on the DB-backed stats")

    app = _db_app(tmp_path)
    monkeypatch.setattr(notes_search, "iter_vault_md", _boom)
    client = await aiohttp_client(app)
    j = await (
        await client.get(
            "/api/portal/notes/stats", headers={"Remote-User": "household"}
        )
    ).json()
    assert j["ok"]
    tags = {t["value"]: t["count"] for t in j["tags"]}
    assert tags.get("garten") == 3  # 3 distinct (session, message) mentions
    assert tags.get("urlaub") == 1
    assert "nuranna" not in tags  # anna-scoped, not the household caller's
    persons = {p["value"]: p["count"] for p in j["persons"]}
    assert persons.get("anna") == 1
    # Categories from concepts.okf_path folders.
    cats = {c["value"]: c["count"] for c in j["categories"]}
    assert cats.get("okf/people") == 1 and cats.get("okf/events") == 1
    # A dense 12-month growth series from concepts.updated.
    assert len(j["months"]) == 12
    key = datetime.now(timezone.utc).strftime("%Y-%m")
    assert any(m["month"] == key and m["count"] == 2 for m in j["months"])
    # Most-linked from event_entities edge counts.
    linked = {link_["value"]: link_["count"] for link_ in j["linked"]}
    assert linked.get("Anna") == 1


async def test_overview_falls_back_without_projection(aiohttp_client, tmp_path):
    # No solaris.db projection → the vault-scan path still serves the overview.
    client = await aiohttp_client(_app(tmp_path))
    j = await (
        await client.get("/api/portal/notes", headers={"Remote-User": "household"})
    ).json()
    assert j["ok"]
    assert j["counts"]["facts"] == 2

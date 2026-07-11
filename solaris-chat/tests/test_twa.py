"""Android TWA prerequisites (#716): the /.well-known/assetlinks.json route
and the PWA/TWA icons declared in manifest.json."""

from __future__ import annotations

import json
import struct

from solaris_chat.server import STATIC_DIR, build_app


class _FakeHermes:
    """Minimal engine stub — the assetlinks route never touches it."""


async def test_assetlinks_is_public_json_array(aiohttp_client):
    # No auth header at all (like /sw.js): Google's verifier is unauthenticated.
    app = build_app(
        hermes=_FakeHermes(),
        remote_user_header="Remote-User",
        default_uid="household",
    )
    client = await aiohttp_client(app)

    resp = await client.get("/.well-known/assetlinks.json")
    assert resp.status == 200
    assert resp.headers["Content-Type"] == "application/json; charset=utf-8"
    body = await resp.json()
    assert isinstance(body, list)


async def test_assetlinks_empty_without_fingerprints(aiohttp_client):
    app = build_app(
        hermes=_FakeHermes(),
        remote_user_header="Remote-User",
        default_uid="household",
        android_cert_fingerprints=(),
    )
    client = await aiohttp_client(app)

    resp = await client.get("/.well-known/assetlinks.json")
    assert resp.status == 200
    assert await resp.json() == []


async def test_assetlinks_carries_configured_fingerprint(aiohttp_client):
    app = build_app(
        hermes=_FakeHermes(),
        remote_user_header="Remote-User",
        default_uid="household",
        android_package="cloud.dopp.solaris",
        android_cert_fingerprints=("AA:BB:CC",),
    )
    client = await aiohttp_client(app)

    resp = await client.get("/.well-known/assetlinks.json")
    assert resp.status == 200
    statement = await resp.json()
    assert len(statement) == 1
    target = statement[0]["target"]
    assert statement[0]["relation"] == ["delegate_permission/common.handle_all_urls"]
    assert target["namespace"] == "android_app"
    assert target["package_name"] == "cloud.dopp.solaris"
    assert target["sha256_cert_fingerprints"] == ["AA:BB:CC"]


def _png_dims(path):
    """(width, height) from the PNG magic bytes + IHDR — no image lib needed."""
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n", f"{path} is not a PNG"
    width, height = struct.unpack(">II", data[16:24])
    return width, height


def test_pwa_icon_files_exist_with_right_dimensions():
    for name, size in [
        ("icon-192.png", 192),
        ("icon-512.png", 512),
        ("icon-512-maskable.png", 512),
    ]:
        path = STATIC_DIR / name
        assert path.exists(), f"missing {name}"
        assert _png_dims(path) == (size, size)


def test_manifest_declares_the_pwa_icons():
    manifest = json.loads((STATIC_DIR / "manifest.json").read_text(encoding="utf-8"))
    icons = {
        (i["src"], i.get("purpose")): i
        for i in manifest["icons"]
        if i["type"] == "image/png"
    }
    assert icons[("/static/icon-192.png", "any")]["sizes"] == "192x192"
    assert icons[("/static/icon-512.png", "any")]["sizes"] == "512x512"
    assert icons[("/static/icon-512-maskable.png", "maskable")]["sizes"] == "512x512"

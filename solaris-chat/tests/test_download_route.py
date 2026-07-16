"""The /download companion-app link (www.dopp.cloud/download → latest signed APK).

The route 302-redirects to GitHub's `releases/latest/download/app-release.apk`,
so it always resolves to the newest CI-published build without a per-release
edit. Here we lock the redirect target; the route wiring itself is a one-line
`add_get("/download", download)` + `raise web.HTTPFound(ANDROID_APK_URL)`.
"""

from __future__ import annotations

from solaris_chat.server import ANDROID_APK_URL


def test_apk_url_is_the_latest_signed_release_asset():
    # `releases/latest/download/<asset>` is GitHub's always-newest redirect, so
    # the link never needs bumping on a new release.
    assert ANDROID_APK_URL == (
        "https://github.com/mdopp/solaris-android"
        "/releases/latest/download/app-release.apk"
    )
    assert ANDROID_APK_URL.startswith("https://")
    assert "/releases/latest/download/" in ANDROID_APK_URL
    assert ANDROID_APK_URL.endswith(".apk")

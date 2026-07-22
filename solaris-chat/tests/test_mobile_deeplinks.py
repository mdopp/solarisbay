"""Frontend-contract checks for the mobile deep-link changes (#765/#766/#769).

Source-text asserts over the single-file PWA (index.html) lock the markup/JS
contract; the real behaviour is box-verified. Covers: the removed standalone
Energie page (now the `.energy` dot-command, #973) + the de-duped Chats-page nav
(#765), the `?ask=` household deep link (#766), and the
`#/p/device/<entity_id>` single-device route (#769).
"""

from __future__ import annotations

from solaris_chat.server import STATIC_DIR

_HTML = (STATIC_DIR / "index.html").read_text(encoding="utf-8")


def test_energie_standalone_page_removed():
    # #973: energy is no longer a standalone page/tab — it lives in the `.energy`
    # dot-command. The bottom-tab + rail + route are gone; the renderers stay.
    assert 'id="tab-energy"' not in _HTML
    assert 'id="rail-energy"' not in _HTML
    assert "tabEnergy" not in _HTML
    assert "railEnergy" not in _HTML
    assert '"#/p/energy"' not in _HTML
    # Renderers reused by `.energy` remain.
    assert "function renderEnergyPage(card, e)" in _HTML
    assert "function drawEnergyChart(body, series)" in _HTML
    assert 'else if (cmd === "energy") buildEnergyCard(card);' in _HTML


def test_chats_page_nav_deduped_on_mobile_only():
    # #765: the redundant primary nav group at the top of the Chats full-page
    # view is hidden on mobile (the bottom tab bar replaces it); desktop keeps
    # the rail-nav — the rule lives inside the mobile media query.
    assert ".rail-nav { display: none; }" in _HTML


def test_ask_param_household_deep_link():
    # #766: `#/?ask=<urlencoded>` opens a NEW household chat and AUTO-SENDS the
    # decoded text, consuming the param once (strip before send → no double-send).
    assert "function consumeAskParam()" in _HTML
    assert 'get("ask")' in _HTML
    assert "history.replaceState(null" in _HTML  # consume once
    assert "pendingTopic = HOUSEHOLD_TOPIC;" in _HTML
    assert "runTurn(text, []);" in _HTML
    assert "if (!consumeAskParam()) routeFromLocation();" in _HTML
    # The chosen scheme is documented in a code comment for the Android app.
    assert "#/?ask=<urlencodierter-text>" in _HTML


def test_single_device_route():
    # #769: #/p/device/<entity_id> opens a one-device page reusing renderHaCard
    # over the /api/portal/state card.
    assert (
        'if (type.indexOf("device/") === 0) { openDevicePage(type.slice(7)); return; }'
        in _HTML
    )
    assert "function openDevicePage(entityId)" in _HTML
    assert "/api/portal/state?entity_id=" in _HTML
    assert "renderHaCard(j.card, false, { pin: true })" in _HTML


def test_single_camera_route():
    # #782: #/p/camera/<entity_id> opens a page showing one camera's live HA
    # snapshot (replaces the #770 placeholder), served to the browser/Authelia
    # session via the /api/portal/camera/<id>/snapshot twin.
    assert (
        'if (type.indexOf("camera/") === 0) { openCameraPage(type.slice(7)); return; }'
        in _HTML
    )
    assert "function openCameraPage(entityId)" in _HTML
    assert '"/api/portal/camera/" + encodeURIComponent(entityId) + "/snapshot"' in _HTML
    # The still is refreshed on a timer that is torn down when the route leaves.
    assert "cameraTimer = setInterval(refresh, 5000)" in _HTML
    assert "function stopCameraRefresh()" in _HTML
    server_src = (STATIC_DIR.parent / "server.py").read_text(encoding="utf-8")
    # The browser/Authelia session reaches the snapshot on /api/ (the /napi/
    # twin is device-token only), so the /api/ GET route must be registered.
    assert '"/api/portal/camera/{entity_id}/snapshot", portal_camera_snapshot' in (
        server_src
    )


def test_state_route_registered_for_browser_session():
    # #769: /api/portal/state must be reachable on the Authelia session (not only
    # the /napi/ device-token twin) so the deep-link route can fetch the card.
    server_src = (STATIC_DIR.parent / "server.py").read_text(encoding="utf-8")
    assert 'app.router.add_get("/api/portal/state", portal_state)' in server_src

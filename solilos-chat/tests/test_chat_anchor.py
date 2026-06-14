"""Frontend-contract checks for bottom-anchoring the transcript (#412).

The message list hugs the composer: a short/new chat sits at the bottom with
empty space above, and new messages push older ones up. On mobile an opening
keyboard must keep the latest messages + composer visible instead of leaving an
empty gap. These lock the markup/JS contract; the real check is the box-verify.
"""

from __future__ import annotations

import re

from solilos_chat.server import STATIC_DIR

_HTML = (STATIC_DIR / "index.html").read_text(encoding="utf-8")


def test_log_first_child_pushed_to_bottom():
    # The transcript bottom-anchors via margin-top:auto on the first child (so a
    # short chat hugs the composer) rather than justify-content:flex-end, which
    # would make overflowing top content unreachable.
    assert "#log > :first-child { margin-top: auto; }" in _HTML
    log_rule = re.search(r"#log \{([^}]*)\}", _HTML)
    assert log_rule, "#log rule not found"
    body = log_rule.group(1)
    # overflow-y:auto stays so scroll-up-to-read keeps working.
    assert "overflow-y: auto" in body
    assert "justify-content: flex-end" not in body


def test_visual_viewport_fits_shell_above_keyboard():
    # A visualViewport resize listener pins body to the visible height so the
    # composer + latest messages stay above the mobile keyboard, re-sticking the
    # transcript to the bottom.
    assert "if (window.visualViewport) {" in _HTML
    fit = re.search(
        r"var fitViewport = function \(\) \{(.*?)\n        \};", _HTML, re.S
    )
    assert fit, "fitViewport handler not found"
    body = fit.group(1)
    assert "var stick = atBottom();" in body
    assert 'document.body.style.height = window.visualViewport.height + "px";' in body
    assert "scrollDown(stick);" in body
    assert 'window.visualViewport.addEventListener("resize", fitViewport);' in _HTML

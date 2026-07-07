"""markdown.js contract: entity-id tokens survive the emphasis rules (#693).

The `_.._` italic rule used to fire inside snake_case entity_ids, turning
`light.dimmer_2_5` into `light.dimmer<em>2</em>5` and producing a broken link.
Entity-id-like tokens and `[[..]]` wikilink spans are now sentinel-protected
(like inline code) so they render verbatim, and a model-emitted
`[label](entity_id)` link lands on the concept page `#/c/<id>` instead of an
external URL. Executes markdown.js in node so the assertion fails against the
unfixed renderer.
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from solaris_chat.server import STATIC_DIR

_MD_JS = STATIC_DIR / "markdown.js"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node not available"
)


def _render(src: str) -> str:
    script = (
        "global.window = {};\n"
        f"require({json.dumps(str(_MD_JS))});\n"
        "let src = JSON.parse(process.argv[1]);\n"
        "process.stdout.write(global.window.renderMarkdown(src));\n"
    )
    out = subprocess.run(
        ["node", "-e", script, json.dumps(src)],
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout


def test_entity_id_survives_italic_rule():
    html = _render("Das Sofalicht ist light.dimmer_2_5 an.")
    assert "light.dimmer_2_5" in html
    assert "<em>" not in html


def test_entity_id_markdown_link_maps_to_concept_page():
    html = _render("[Sofalicht](light.dimmer_2_5)")
    assert 'href="#/c/light.dimmer_2_5"' in html
    assert "http" not in html


def test_wikilink_span_survives_verbatim():
    html = _render("siehe [[light.dimmer_2_5|Sofa]]")
    assert "[[light.dimmer_2_5|Sofa]]" in html
    assert "<em>" not in html


def test_normal_emphasis_still_works():
    html = _render("das ist _wichtig_ und **fett** hier")
    assert "<em>wichtig</em>" in html
    assert "<strong>fett</strong>" in html


def test_external_link_stays_external():
    html = _render("[Google](https://google.com)")
    assert 'href="https://google.com"' in html
    assert 'target="_blank"' in html

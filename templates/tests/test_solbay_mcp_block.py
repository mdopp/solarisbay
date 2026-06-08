"""Tests for solbay's mcp_servers block rewrite: the household-facing
config carries servicebay-mcp + gatekeeper-mcp and NEVER admin-soul's
`servicebay_admin` entry (#268 TTFT trim).

Hermes' mcp_servers block is global to the one instance, so any entry left
in the shared config.yaml bloats every household chat turn's prefill.
admin-soul's `servicebay_admin` is a ~6.3k-token near-duplicate of
`servicebay-mcp` that only the operator soul needs; a household-stack
redeploy must keep it out of the household-facing config. admin-soul's own
post-deploy re-splices it when IT is deployed (covered separately by
test_admin_soul_token.py) — that wiring stays intact.

Like the sibling token tests, the hyphenated post-deploy.py is loaded via
importlib; no live ServiceBay / hermes container is touched.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

TEMPLATES = pathlib.Path(__file__).resolve().parents[1]

GOOD = "sb_0a1b2c3d_ABCDEF234567"
ADMIN = "sb_cccccccc_DDDD234567"


def _load(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def solbay():
    return _load("solbay_post_deploy", TEMPLATES / "solilos" / "post-deploy.py")


# A live config the box could carry after admin-soul has spliced its entry
# into the shared block: household entries plus servicebay_admin.
_CONFIG_WITH_ADMIN = (
    "timezone: Europe/Berlin\n"
    "model:\n"
    "  provider: custom\n"
    "mcp_servers:\n"
    "  servicebay-mcp:\n"
    '    url: "http://127.0.0.1:5888/mcp"\n'
    "    headers:\n"
    f'      Authorization: "Bearer {GOOD}"\n'
    "  gatekeeper-mcp:\n"
    '    url: "http://127.0.0.1:10760/mcp"\n'
    "  servicebay_admin:\n"
    '    url: "http://127.0.0.1:5888/mcp"\n'
    "    headers:\n"
    f'      Authorization: "Bearer {ADMIN}"\n'
    "display:\n"
    "  personality: default\n"
)

_HOUSEHOLD_SERVERS = [
    ("servicebay-mcp", "http://127.0.0.1:5888/mcp", GOOD),
    ("gatekeeper-mcp", "http://127.0.0.1:10760/mcp", ""),
]


def test_strip_drops_admin_entry(solbay):
    """strip_mcp_servers_block removes the whole prior block, admin included."""
    stripped = solbay.strip_mcp_servers_block(_CONFIG_WITH_ADMIN)
    assert "servicebay_admin" not in stripped
    assert "mcp_servers:" not in stripped
    # Non-mcp content is preserved.
    assert "timezone: Europe/Berlin" in stripped
    assert "display:" in stripped


def test_rendered_block_is_household_only(solbay):
    block = solbay.render_mcp_block(_HOUSEHOLD_SERVERS)
    assert "servicebay-mcp:" in block
    assert "gatekeeper-mcp:" in block
    assert "servicebay_admin" not in block
    assert f"Bearer {ADMIN}" not in block


def test_merge_rewrites_household_block_without_admin(solbay, monkeypatch):
    written = {}
    monkeypatch.setattr(solbay, "read_config_via_container", lambda: _CONFIG_WITH_ADMIN)
    monkeypatch.setattr(
        solbay,
        "write_config_via_container",
        lambda c: written.update(c=c) or True,
    )

    assert solbay.merge_config_yaml(_HOUSEHOLD_SERVERS) is True
    out = written["c"]
    assert "servicebay-mcp:" in out
    assert "gatekeeper-mcp:" in out
    # The whole point: the operator-soul entry is gone from the household config.
    assert "servicebay_admin" not in out
    assert f"Bearer {ADMIN}" not in out
    # The household token survives.
    assert f"Bearer {GOOD}" in out

"""Who may reach the model server (#1344, #1416).

There is no authentication anywhere here, so the endpoint is on-box only — but
"on-box" is two addresses, not one. A pod on host networking (the Solaris
Engine, the health check) uses `127.0.0.1`; an isolated pod (claude-dev's
`pi`) can only use `host.containers.internal`, which rootless podman/pasta
maps to the host's LAN address rather than to loopback. A loopback bind
therefore serves the first and silently starves the second.

ADR-0007 Decision 3 resolves that by binding wider (#1344). The wide bind is
load-bearing and does not show up as a failure when it is missing: `pi`'s model
picker is merely empty.

The LAN is a separate decision and, since #1420, an explicit one: `LLAMA_PORT`
carries `blockLanAccess: false`, so ServiceBay leaves the port out of its host
block set and every device in the home network reaches an unauthenticated model
server. The operator asked for that. What is asserted here is that the file
still says so — the flag on its own reads like an oversight.

Since #1416 the process carrying that wide bind is the mode policy proxy, and
llama-server itself sits behind it on loopback `LLAMA_ROUTER_PORT`. That is
the same carve-out one layer in, and it is what makes the mode enforceable:
a router on `0.0.0.0` could be asked for any preset by any sibling pod.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys

import pytest

TEMPLATES = pathlib.Path(__file__).resolve().parents[1]
LLAMA = TEMPLATES / "llama"


def _load(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def pd():
    return _load("llama_pd_pod_access", LLAMA / "post-deploy.py")


@pytest.fixture(scope="module")
def template_text() -> str:
    return (LLAMA / "template.yml").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def variables() -> dict:
    return json.loads((LLAMA / "variables.json").read_text(encoding="utf-8"))


def test_the_router_binds_loopback_only(template_text):
    """The policy proxy carries the wide bind now. A router on `0.0.0.0` would
    be reachable from every sibling pod, and the mode would mean nothing."""
    assert '- "--host"\n    - "127.0.0.1"' in template_text
    assert '- "0.0.0.0"' not in template_text


def test_post_deploy_bind_mirrors_the_template(pd):
    args = pd.server_args("11434", "/models")
    assert args[:2] == ["--host", "127.0.0.1"]


def test_the_proxy_carries_the_wide_bind_the_isolated_pods_need(pd, tmp_path):
    """ADR-0007 Decision 3, one layer in: `host.containers.internal` resolves
    to the host's LAN address, so the thing on LLAMA_PORT still has to bind
    every interface — it is just the proxy rather than the router now."""
    server = pd.make_proxy_server(str(tmp_path), 0, 11434)
    try:
        assert server.server_address[0] == "0.0.0.0"
    finally:
        server.server_close()


def test_no_preset_can_move_the_bind(pd):
    """In router mode (#1416) the bind lives on the router's own argv and a
    preset only describes a model — but a `host=` line in the presets file
    would be inherited by the child and take `pi` away for the window."""
    text = pd.render_presets("/models")
    assert "host=" not in text and "port=" not in text


def test_the_lan_is_opened_on_purpose_and_says_so(variables):
    """#1420 — the operator asked for llama to serve its models in the LAN
    without a login, and until then the reachability rested on a hand-written
    nftables `accept` that the next deploy could overwrite.

    The flag alone is half of it. An unauthenticated model server open to the
    whole home network is the kind of thing a later reader reverts on sight, so
    the description has to carry the decision and the cost — asserted here
    because a rewrite that drops them leaves a file saying nothing while the
    port stays open."""
    port = variables["LLAMA_PORT"]
    assert port["blockLanAccess"] is False
    description = port["description"]
    assert "no authentication" in description
    assert "deliberately" in description
    assert "#1420" in description


def test_port_is_declared_so_the_firewall_rule_has_a_target(template_text):
    assert 'servicebay.ports: "{{LLAMA_PORT}}/tcp"' in template_text


def test_health_check_stays_on_loopback(template_text):
    """The check runs in the host netns (ADR 0007), and loopback is the path
    the Engine uses — probing the LAN address would test the firewall rule
    instead of the server. LLAMA_PORT rather than the router port, so a dead
    proxy reads as a dead service: it is the only door consumers have."""
    assert "url: http://127.0.0.1:{{LLAMA_PORT}}/health" in template_text


def test_schema_version_bumped_for_router_mode(template_text):
    assert 'servicebay.schema-version: "3"' in template_text


def test_readme_names_all_three_audiences():
    readme = (LLAMA / "README.md").read_text(encoding="utf-8")
    assert "host.containers.internal" in readme
    assert "blockLanAccess" in readme
    assert "solaris-llama-policy.service" in readme
    # #1420: the LAN is the fourth audience now, and the README is where the
    # reasoning lives — the flag is one word and cannot carry it.
    assert "blockLanAccess: false" in readme
    assert "no authentication" in readme

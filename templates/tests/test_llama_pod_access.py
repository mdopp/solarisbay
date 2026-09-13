"""Who may reach the model server (#1344, #1416).

There is no authentication anywhere here, so the endpoint is on-box only — but
"on-box" is two addresses, not one. A pod on host networking (the Solaris
Engine, the health check) uses `127.0.0.1`; an isolated pod (claude-dev's
`pi`) can only use `host.containers.internal`, which rootless podman/pasta
maps to the host's LAN address rather than to loopback. A loopback bind
therefore serves the first and silently starves the second.

ADR-0007 Decision 3 resolves that by binding wider and closing the LAN one
layer down: `LLAMA_PORT` carries `blockLanAccess: true`, and ServiceBay drops
the port on physical interfaces while leaving `lo` — where the pasta-proxied
pod path lands — alone. Both halves are load-bearing and neither shows up as
a failure when it is missing: without the wide bind `pi`'s model picker is
merely empty, and without the flag an unauthenticated model server answers
the whole LAN.

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


def test_port_blocks_lan_access(variables):
    assert variables["LLAMA_PORT"]["blockLanAccess"] is True


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

"""Tests for the Sol-Engine-era solilos Pod topology (Phase 4 decommission).

The Pod runs exactly TWO app containers: `chat` (the engine — agent loop,
chat surface, Ollama facade for HA, scheduler, crons) and `gatekeeper` (the
Wyoming bridge for wyoming-satellite hardware). Hermes, the config-agent
sidecar, the trace side-pod and the admin-soul idle container are gone;
their duties live in-process in the engine. Validates template.yml +
variables.json wiring.
"""

from __future__ import annotations

import json
import pathlib
import re

import pytest

TEMPLATES = pathlib.Path(__file__).resolve().parents[1]
SOLILOS = TEMPLATES / "solilos"


@pytest.fixture(scope="module")
def raw_template() -> str:
    return (SOLILOS / "template.yml").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def variables() -> dict:
    return json.loads((SOLILOS / "variables.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def post_deploy_src() -> str:
    return (SOLILOS / "post-deploy.py").read_text(encoding="utf-8")


# The CI templates job runs stdlib-only (`pip install pytest`, no PyYAML), so
# we navigate the Pod by its `- name: <x>` blocks instead of parsing YAML.


def _section(raw: str, key: str) -> str:
    body = raw.split(f"\n  {key}:\n", 1)[1]
    return re.split(r"\n  [a-zA-Z]", body, 1)[0]


def _names(section: str) -> list[str]:
    return re.findall(r"^  - name: ([\w-]+)", section, re.MULTILINE)


def _block(raw: str, name: str) -> str:
    after = raw.split(f"  - name: {name}\n", 1)[1]
    return re.split(r"\n  (?:- |[a-zA-Z])", after, 1)[0]


def _mounts(block: str) -> list[str]:
    vm = block.split("volumeMounts:", 1)
    if len(vm) == 1:
        return []
    return re.findall(r"name: ([\w-]+)", vm[1])


def test_exactly_chat_and_gatekeeper_containers(raw_template):
    assert _names(_section(raw_template, "containers")) == ["chat", "gatekeeper"]


def test_hermes_era_containers_gone(raw_template):
    # The admin-soul *skill volume* legitimately stays (the engine's prompt
    # assembly reads it); the retired idle container ran `sleep infinity`.
    for retired in ("hermes-agent", "config-agent", "trace_proxy", "sleep"):
        assert retired not in raw_template
    assert "nousresearch" not in raw_template


def test_init_containers_are_notes_perms_and_schema_init(raw_template):
    assert _names(_section(raw_template, "initContainers")) == [
        "notes-perms",
        "schema-init",
    ]


def test_chat_carries_engine_env(raw_template):
    chat = _block(raw_template, "chat")
    for needed in (
        "SOL_API_KEY",
        "SOUL_PATH",
        "ADMIN_SOUL_PATH",
        "ADMIN_SKILLS_DIR",
        "SB_MCP_URL",
        "SB_MCP_TOKEN_PATH",
        "HASS_URL",
        "HASS_TOKEN",
        "OLLAMA_URL",
        "FAST_MODEL",
        "THOROUGH_MODEL",
    ):
        assert needed in chat, f"chat env missing {needed}"
    # The Hermes-era wiring must not resurface.
    for gone in ("HERMES_URL", "HERMES_ADMIN_URL", "CONFIG_AGENT_URL", "TRACE_PROXY"):
        assert gone not in chat


def test_chat_binds_loopback(raw_template):
    chat = _block(raw_template, "chat")
    assert 'value: "127.0.0.1"' in chat


def test_gatekeeper_speaks_the_engine_facade(raw_template):
    gk = _block(raw_template, "gatekeeper")
    assert "SOL_ENGINE_URL" in gk
    assert "http://127.0.0.1:{{CHAT_PORT}}/ollama" in gk
    assert "SOL_API_KEY" in gk


def test_gatekeeper_has_no_admin_access(raw_template):
    gk = _block(raw_template, "gatekeeper")
    assert "ADMIN" not in gk.replace("GATEKEEPER_MCP_TOKEN", "")


def test_ports_annotation_lists_chat_and_gatekeeper(raw_template):
    assert (
        'servicebay.ports: "{{CHAT_PORT}}/tcp,{{GATEKEEPER_PORT}}/tcp"' in raw_template
    )


def test_all_mounts_resolve_to_declared_volumes(raw_template):
    volumes = set(_names(_section(raw_template, "volumes")))
    for container in ("chat", "gatekeeper", "notes-perms", "schema-init"):
        for mount in _mounts(_block(raw_template, container)):
            assert mount in volumes, f"{container} mounts undeclared volume {mount}"


def test_variables_renamed_to_engine_era(variables):
    assert "SOL_API_KEY" in variables
    assert variables["SOL_API_KEY"]["type"] == "secret"
    assert variables["CHAT_PORT"]["default"] == "8787"
    assert "CHAT_IMAGE" in variables
    for gone in (
        "HERMES_API_PORT",
        "HERMES_ADMIN_API_PORT",
        "HERMES_DEEP_API_PORT",
        "HERMES_API_KEY",
        "HERMES_LLM_PROVIDER_URL",
        "TRACE_PROXY_PORT",
        "CONFIG_AGENT_URL",
        "HERMES_DASHBOARD_PORT",
        "TELEGRAM_BOT_TOKEN",
        "FAST_HERMES_MODEL",
        "ADMIN_SOUL_IMAGE",
    ):
        assert gone not in variables, f"stale variable {gone}"


def test_model_map_defaults(variables):
    assert variables["FAST_MODEL"]["default"] == "gemma4:e2b"
    assert variables["THOROUGH_MODEL"]["default"] == "gemma4:12b"


def test_post_deploy_is_engine_era(post_deploy_src):
    # The Hermes-era machinery must be gone from the deploy hook.
    for gone in (
        "hermes gateway",
        "profile use",
        "no-bundled-skills",
        "config.yaml",
        "register_chronicle_cron",
        "write_gateway_env",
    ):
        assert gone not in post_deploy_src, f"hermes-era residue: {gone}"
    # The Phase-2/3 wiring must be present.
    for needed in (
        "wire_voice_pipeline",
        "ensure_wyoming_entry",
        "ensure_conversation_agent",
        "ensure_assist_pipeline",
        "ensure_admin_token_file",
    ):
        assert needed in post_deploy_src

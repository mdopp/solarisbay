"""Tests for the instance-per-profile Hermes topology (#293b): the solilos Pod
runs one gateway container per profile — `hermes-household` (`-p household`) on
HERMES_API_PORT and `hermes-admin` (`-p admin`) on HERMES_ADMIN_API_PORT —
sharing the same volumes. Validates the template.yml as parseable YAML and the
new HERMES_ADMIN_API_PORT variable.
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


# The CI templates job runs stdlib-only (`pip install pytest`, no PyYAML), so
# we navigate the Pod by its `- name: <x>` blocks instead of parsing YAML. A
# block runs from its `- name:` line up to the next list item at the same or a
# shallower indent (the next container, or the `volumes:`/`containers:` key).


def _section(raw: str, key: str) -> str:
    # The body of a top-level spec key (`containers:` / `initContainers:` /
    # `volumes:`) up to the next top-level (2-space) key.
    body = raw.split(f"\n  {key}:\n", 1)[1]
    return re.split(r"\n  [a-zA-Z]", body, 1)[0]


def _names(section: str) -> list[str]:
    return re.findall(r"^  - name: ([\w-]+)", section, re.MULTILINE)


def _block(raw: str, name: str) -> str:
    after = raw.split(f"  - name: {name}\n", 1)[1]
    # Up to the next sibling list item (`  - ` at 2-space indent) or the next
    # top-level spec key.
    return re.split(r"\n  (?:- |[a-zA-Z])", after, 1)[0]


def _mounts(block: str) -> list[str]:
    vm = block.split("volumeMounts:", 1)
    if len(vm) == 1:
        return []
    return re.findall(r"name: ([\w-]+)", vm[1])


# ── the two gateway containers exist (and the placeholder is gone) ──────────


def test_both_gateway_containers_present(raw_template):
    names = _names(_section(raw_template, "containers"))
    assert "hermes-household" in names
    assert "hermes-admin" in names


def test_admin_soul_placeholder_retired(raw_template):
    names = _names(_section(raw_template, "containers"))
    assert "admin-soul" not in names


def test_single_hermes_container_replaced(raw_template):
    # The old single `hermes` container is gone — split into the two profiles.
    assert "hermes" not in _names(_section(raw_template, "containers"))


# ── each gateway runs its own profile ───────────────────────────────────────


def _profile_args(block: str) -> list[str]:
    # Anchor on the `args:` *key* (a comment may mention "args:" earlier).
    args = re.split(r"\n    args:\n", block, 1)[1]
    args = re.split(r"\n    [a-zA-Z]", args, 1)[0]
    return re.findall(r"^    - ([\w-]+)", args, re.MULTILINE)


def test_household_runs_household_profile_gateway(raw_template):
    hh = _block(raw_template, "hermes-household")
    assert _profile_args(hh) == ["-p", "household", "gateway", "run"]
    assert "image: docker.io/nousresearch/hermes-agent:latest" in hh


def test_admin_runs_admin_profile_gateway(raw_template):
    adm = _block(raw_template, "hermes-admin")
    assert _profile_args(adm) == ["-p", "admin", "gateway", "run"]
    assert "image: docker.io/nousresearch/hermes-agent:latest" in adm


# ── distinct API ports; only household runs the dashboard ───────────────────


def test_gateways_bind_distinct_api_ports(raw_template):
    hh_block = raw_template.split("- name: hermes-household")[1].split(
        "- name: config-agent"
    )[0]
    adm_block = raw_template.split("- name: hermes-admin")[1]
    assert "{{HERMES_API_PORT}}" in hh_block
    assert "{{HERMES_ADMIN_API_PORT}}" in adm_block
    # The household port must not leak into the admin gateway env, nor vice
    # versa, or both gateways would race the same host port.
    assert "{{HERMES_ADMIN_API_PORT}}" not in hh_block
    assert "{{HERMES_API_PORT}}" not in adm_block


def test_only_household_runs_dashboard(raw_template):
    hh_block = raw_template.split("- name: hermes-household")[1].split(
        "- name: config-agent"
    )[0]
    adm_block = raw_template.split("- name: hermes-admin")[1]
    assert "name: HERMES_DASHBOARD" in hh_block
    assert "name: HERMES_DASHBOARD" not in adm_block


# ── both gateways share the same household data volumes ─────────────────────


def test_both_gateways_share_core_volumes(raw_template):
    shared = {"hermes-data", "syncthing-notes", "solilos-data"}
    for name in ("hermes-household", "hermes-admin"):
        mounts = set(_mounts(_block(raw_template, name)))
        assert shared <= mounts, (name, sorted(mounts))


def test_admin_gateway_carries_ha_and_ollama_env(raw_template):
    adm_block = raw_template.split("- name: hermes-admin")[1]
    assert "HASS_URL" in adm_block
    assert "HASS_TOKEN" in adm_block
    assert "OPENAI_BASE_URL" in adm_block


# ── every mount resolves to a declared volume; no orphan volumes ────────────


def test_all_mounts_resolve_to_declared_volumes(raw_template):
    declared = set(_names(_section(raw_template, "volumes")))
    for section in ("initContainers", "containers"):
        body = _section(raw_template, section)
        for name in _names(body):
            for mount in _mounts(_block(raw_template, name)):
                assert mount in declared, (name, mount)


# ── the new admin-API-port variable + the ports annotation ──────────────────


def test_admin_api_port_variable_defined(variables):
    assert "HERMES_ADMIN_API_PORT" in variables
    assert variables["HERMES_ADMIN_API_PORT"]["default"] == "8643"


def test_admin_api_port_distinct_from_household(variables):
    assert (
        variables["HERMES_ADMIN_API_PORT"]["default"]
        != variables["HERMES_API_PORT"]["default"]
    )


def test_ports_annotation_raw_lists_admin_port(raw_template):
    ann = raw_template.split("servicebay.ports:")[1].splitlines()[0]
    assert "{{HERMES_ADMIN_API_PORT}}" in ann
    assert "{{HERMES_API_PORT}}" in ann


def test_admin_soul_image_variable_removed(variables):
    # The busybox placeholder image is no longer referenced — its wizard var is
    # gone so the operator isn't prompted for a now-unused image.
    assert "ADMIN_SOUL_IMAGE" not in variables


# ── voice routing: the gatekeeper targets the HOUSEHOLD gateway only (#293d) ──


def _gatekeeper_block(raw_template: str) -> str:
    # The gatekeeper container env, bounded at the next container (hermes-admin
    # follows it and legitimately carries the admin port — don't bleed into it).
    after = raw_template.split("- name: gatekeeper")[1]
    return after.split("- name: hermes-admin")[0]


def test_gatekeeper_targets_household_gateway(raw_template):
    gk_block = _gatekeeper_block(raw_template)
    hermes_url = next(
        line
        for line in gk_block.splitlines()
        if "value:" in line and "HERMES_API_PORT" in line
    )
    # Voice rides the household gateway (HERMES_API_PORT / :8642), never admin.
    assert "{{HERMES_API_PORT}}" in hermes_url


def test_gatekeeper_has_no_admin_gateway_access(raw_template):
    gk_block = _gatekeeper_block(raw_template)
    # Residents speak to Sol on the household profile; the gatekeeper must carry
    # neither an admin URL env nor the admin gateway port, so a voice turn can
    # never reach hermes-admin (:8643).
    assert "name: HERMES_ADMIN_URL" not in gk_block
    assert "{{HERMES_ADMIN_API_PORT}}" not in gk_block


def test_gatekeeper_fast_model_uses_fast_hermes_model_var(raw_template):
    gk_block = _gatekeeper_block(raw_template)
    fast = next(
        line
        for line in gk_block.splitlines()
        if "value:" in line and "FAST_HERMES_MODEL" in line
    )
    # The fast voice model is the wizard var, whose default (gemma4:e2b) aligns
    # with the household profile's pinned model (#293a).
    assert "{{FAST_HERMES_MODEL}}" in fast


def test_fast_hermes_model_default_matches_household_profile(variables):
    assert variables["FAST_HERMES_MODEL"]["default"] == "gemma4:e2b"

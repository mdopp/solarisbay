"""Tests for the corrected single-container multi-profile Hermes topology
(#293). The solilos Pod runs ONE hermes container with `args: [gateway, run]`
(NEVER `command:` with `-p` — that broke s6 and crash-looped the box, the #294
revert). The bare `gateway run` serves the household persona = the DEFAULT
profile on HERMES_API_PORT, and the SAME container ALSO starts the isolated
`admin` profile's gateway (`hermes -p admin gateway start`, fired from
post-deploy.py) on HERMES_ADMIN_API_PORT — its port pinned by a per-profile
`.env` (API_SERVER_PORT), because the container env overrides any config.yaml
port (box-verified #293). Validates template.yml + variables.json + that
post-deploy.py drives the s6-safe mechanism.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import re
import sys

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


def _load(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def pd():
    return _load("solilos_post_deploy_topology", SOLILOS / "post-deploy.py")


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


# ── ONE hermes container, no second gateway container (the #294 trap avoided) ──


def test_single_hermes_container(raw_template):
    names = _names(_section(raw_template, "containers"))
    assert "hermes" in names
    # No second hermes gateway container — the two-container topology (293b)
    # that crash-looped s6 is dropped entirely (#294 revert).
    assert "hermes-household" not in names
    assert "hermes-admin" not in names


def _hermes_args(block: str) -> list[str]:
    args = re.split(r"\n    args:\n", block, 1)[1]
    args = re.split(r"\n    [a-zA-Z]", args, 1)[0]
    return re.findall(r"^    - ([\w-]+)", args, re.MULTILINE)


def test_hermes_runs_bare_gateway_run_not_profile_command(raw_template):
    hermes = _block(raw_template, "hermes")
    # The s6-safe form: `args: [gateway, run]` (NO `-p`, NO `command:`
    # override). Selecting the profile via `-p` in args/command is the s6 trap
    # that took the box down (#294) — must never appear.
    assert _hermes_args(hermes) == ["gateway", "run"]
    assert "image: docker.io/nousresearch/hermes-agent:latest" in hermes
    # No `command:` key on the hermes container (that bypasses the entrypoint).
    assert not re.search(r"^    command:", hermes, re.MULTILINE)


def test_hermes_container_has_no_profile_flag_anywhere(raw_template):
    hermes = _block(raw_template, "hermes")
    # Guard the whole hermes container block: no `- -p` arg list item that would
    # pin a profile via the CMD (the crash-loop form).
    assert not re.search(r"^    - -p$", hermes, re.MULTILINE)


# ── both gateway ports declared on the single container ──────────────────────


def test_hermes_declares_both_gateway_ports(raw_template):
    hermes = _block(raw_template, "hermes")
    ports = hermes.split("ports:", 1)[1].split("volumeMounts:", 1)[0]
    assert "{{HERMES_API_PORT}}" in ports
    # The in-container admin gateway binds this; declared on the same container.
    assert "{{HERMES_ADMIN_API_PORT}}" in ports


def test_hermes_runs_the_dashboard(raw_template):
    hermes = _block(raw_template, "hermes")
    assert "name: HERMES_DASHBOARD" in hermes


# ── the single container mounts the shared household data ────────────────────


def test_hermes_mounts_shared_data_volumes(raw_template):
    mounts = set(_mounts(_block(raw_template, "hermes")))
    # Both profile gateways run in THIS container, so it carries the shared
    # solilos.db + notes (#293 "auf den gleichen Daten").
    assert {"hermes-data", "syncthing-notes", "solilos-data"} <= mounts


def test_all_mounts_resolve_to_declared_volumes(raw_template):
    declared = set(_names(_section(raw_template, "volumes")))
    for section in ("initContainers", "containers"):
        body = _section(raw_template, section)
        for name in _names(body):
            for mount in _mounts(_block(raw_template, name)):
                assert mount in declared, (name, mount)


# ── post-deploy drives the s6-safe multi-gateway mechanism ───────────────────


def test_post_deploy_makes_household_the_default_profile(pd, post_deploy_src):
    # household IS the default profile: the bare `gateway run` serves it on :8642.
    # No separate household profile is created, and no sticky-default switch.
    assert "hermes_profile_use" not in post_deploy_src
    assert '"use"' not in post_deploy_src
    assert "'use'" not in post_deploy_src
    # provision_profiles touches ONLY the admin profile (household = default).
    src = pd.provision_profiles.__doc__ or ""
    assert "admin" in src.lower()


def test_admin_gateway_boot_hook_mounted_and_written(pd, raw_template, post_deploy_src):
    # The reboot-persistence hook (#299) is written by post-deploy and mounted
    # into cont-init.d, sorting before the image's 02-reconcile-profiles.
    assert "write_admin_gateway_boot_hook()" in post_deploy_src
    hermes = _block(raw_template, "hermes")
    assert "/etc/cont-init.d/016-ensure-admin-gateway" in hermes
    assert "subPath: 016-ensure-admin-gateway" in hermes
    assert "016-ensure-admin-gateway" < "02-reconcile-profiles"


def test_admin_profile_created_no_skills(pd, monkeypatch):
    # `hermes profile create admin --no-skills` — a lean, empty profile (no
    # ~105-skill bundled-catalog copy), the box-verified way to keep it lean.
    calls: list[list[str]] = []

    class _Done:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(
        pd.subprocess, "run", lambda cmd, *a, **k: calls.append(cmd) or _Done()
    )
    assert pd.hermes_profile_create("admin", no_skills=True) is True
    assert calls[-1] == [
        "podman",
        "exec",
        pd.HERMES_CONTAINER,
        "hermes",
        "profile",
        "create",
        "admin",
        "--no-skills",
    ]


def test_post_deploy_starts_admin_gateway_via_start_subcommand(pd, post_deploy_src):
    # The admin gateway is brought up with `gateway start` (not a `-p ... gateway
    # run` CMD), in the same container, AFTER the restart.
    assert "start_admin_gateway()" in post_deploy_src
    src = pd.hermes_gateway_start.__doc__ or ""
    assert "gateway start" in src


def test_gateway_start_uses_dash_p_profile_gateway_start(pd, monkeypatch):
    calls: list[list[str]] = []

    class _Done:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd, *a, **k):
        calls.append(cmd)
        return _Done()

    monkeypatch.setattr(pd.subprocess, "run", fake_run)
    assert pd.hermes_gateway_start("admin") is True
    # The exact s6-safe invocation: `hermes -p admin gateway start` via podman
    # exec — start, never run; -p as a CLI flag, not a container CMD.
    assert calls[-1] == [
        "podman",
        "exec",
        pd.HERMES_CONTAINER,
        "hermes",
        "-p",
        "admin",
        "gateway",
        "start",
    ]


def test_gateway_start_treats_already_running_as_success(pd, monkeypatch):
    class _AlreadyUp:
        returncode = 1
        stdout = "gateway already running"
        stderr = ""

    monkeypatch.setattr(pd.subprocess, "run", lambda *a, **k: _AlreadyUp())
    assert pd.hermes_gateway_start("admin") is True


# ── admin gateway port pinned via per-profile .env (NOT config.yaml, #293) ───


def test_profile_config_carries_no_api_server_port(pd):
    # The container env API_SERVER_PORT overrides any config.yaml port, so the
    # port is NEVER written into config.yaml — it lives in the per-profile .env.
    cfg = pd.render_profile_config_yaml("http://127.0.0.1:11434/v1", "gemma4:12b", [])
    assert "api_server" not in cfg
    assert "platforms" not in cfg


def test_admin_profile_env_pins_port(pd, monkeypatch):
    # write_profile_env_port is the only port lever that works (box-verified) —
    # it writes the per-profile .env via the container.
    writes: dict[str, str] = {}
    monkeypatch.setattr(
        pd, "write_file_in_container", lambda p, c: writes.__setitem__(p, c) or True
    )
    assert pd.write_profile_env_port("admin", "8643") is True
    assert writes["/opt/data/profiles/admin/.env"] == "API_SERVER_PORT=8643\n"


def test_provision_pins_admin_port_via_env_only(pd, monkeypatch):
    writes: dict[str, str] = {}
    monkeypatch.setattr(
        pd, "write_file_in_container", lambda p, c: writes.__setitem__(p, c) or True
    )
    monkeypatch.setattr(pd, "read_file_in_container", lambda p: writes.get(p))
    monkeypatch.setattr(pd, "hermes_profile_create", lambda p, no_skills=False: True)
    monkeypatch.setattr(pd, "admin_mcp_servers", lambda: [])
    monkeypatch.setattr(pd, "symlink_profile_skill", lambda p, s: True)

    pd.provision_profiles("/unused", "http://127.0.0.1:11434/v1", admin_port="8643")

    # The admin gateway's port is in its .env, not its config.yaml.
    assert writes["/opt/data/profiles/admin/.env"] == "API_SERVER_PORT=8643\n"
    assert "api_server" not in writes["/opt/data/profiles/admin/config.yaml"]
    # No household profile is written — household is the default profile.
    assert not any("profiles/household" in p for p in writes)


# ── the admin-API-port variable + the ports annotation ───────────────────────


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


# ── voice routing: the gatekeeper targets the HOUSEHOLD gateway only (#293) ──


def _gatekeeper_block(raw_template: str) -> str:
    after = raw_template.split("- name: gatekeeper")[1]
    return after.split("- name: admin-soul")[0]


def test_gatekeeper_targets_household_gateway(raw_template):
    gk_block = _gatekeeper_block(raw_template)
    hermes_url = next(
        line
        for line in gk_block.splitlines()
        if "value:" in line and "HERMES_API_PORT" in line
    )
    assert "{{HERMES_API_PORT}}" in hermes_url


def test_gatekeeper_has_no_admin_gateway_access(raw_template):
    gk_block = _gatekeeper_block(raw_template)
    # Residents speak to Sol on the household profile; the gatekeeper carries
    # neither an admin URL env nor the admin gateway port.
    assert "name: HERMES_ADMIN_URL" not in gk_block
    assert "{{HERMES_ADMIN_API_PORT}}" not in gk_block


def test_chat_carries_admin_gateway_url(raw_template):
    # The chat proxy is the ONLY component routed to the admin gateway, behind
    # the #209/#229 admin gate.
    chat_block = raw_template.split("- name: chat\n")[1].split("- name: gatekeeper")[0]
    assert "name: HERMES_ADMIN_URL" in chat_block
    assert "{{HERMES_ADMIN_API_PORT}}" in chat_block


def test_fast_hermes_model_default_matches_household_profile(variables):
    assert variables["FAST_HERMES_MODEL"]["default"] == "gemma4:e2b"

"""Tests for the multi-profile Hermes provisioning (#293, native-reconciler).

The household persona IS the DEFAULT profile (served by the bare `gateway run`
on :8642; config = the global config.yaml; solilos skills already mounted), so
provision_profiles creates ONE named profile — `admin` — with `hermes profile
create --no-skills` (lean, no bundled-catalog copy). EVERY per-profile file is
written via the container (the profile dir is hermes-owned 0700 and unwritable
host-side, box-verified #293): config.yaml (ollama model+providers, the recipe
that avoids the openrouter 401; servicebay_admin + servicebay-mcp), its SOUL.md,
and — critically — a per-profile `.env` pinning API_SERVER_PORT (the only port
lever that works; the container env overrides any config.yaml port). The
admin-soul skill pack is symlinked in from the shared bind mount.

The hyphenated post-deploy.py is loaded via importlib (same pattern as the
sibling token tests); no live ServiceBay / hermes container is touched — the
container reads/writes, profile_create, the skill symlink, and the MCP-token
mints are monkeypatched (see the `fc` fake-container fixture) so the logic runs
offline.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

TEMPLATES = pathlib.Path(__file__).resolve().parents[1]

GOOD = "sb_0a1b2c3d_ABCDEF234567"
ADMIN = "sb_cccccccc_DDDD234567"

HOUSEHOLD_SERVERS = [
    ("servicebay-mcp", "http://127.0.0.1:5888/mcp", GOOD),
    ("gatekeeper-mcp", "http://127.0.0.1:10760/mcp", ""),
]
ADMIN_SERVERS = [
    ("servicebay_admin", "http://127.0.0.1:5888/mcp", ADMIN),
    ("servicebay-mcp", "http://127.0.0.1:5888/mcp", GOOD),
]

PROVIDER_URL = "http://127.0.0.1:11434/v1"


def _load(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def pd():
    return _load("solilos_post_deploy", TEMPLATES / "solilos" / "post-deploy.py")


# ── render_ollama_model_block: the load-bearing recipe ──────────────────────


def test_model_block_declares_ollama_provider(pd):
    block = pd.render_ollama_model_block(PROVIDER_URL, "gemma4:e2b")
    assert "model:\n" in block
    assert "  provider: ollama\n" in block
    assert "  model: gemma4:e2b\n" in block
    # The box-confirmed anti-401 recipe: an explicit providers.ollama with the
    # api URL + a (dummy) api_key. Without it the profile 401s on openrouter.
    assert "providers:\n" in block
    assert "  ollama:\n" in block
    assert f"    api: {PROVIDER_URL}\n" in block
    assert '    api_key: "ollama"\n' in block
    # No custom/openrouter provider leaks in.
    assert "provider: custom" not in block
    assert "openrouter" not in block


def test_profile_config_has_household_mcp_and_no_admin(pd):
    content = pd.render_profile_config_yaml(
        PROVIDER_URL, "gemma4:e2b", HOUSEHOLD_SERVERS
    )
    assert "  provider: ollama\n" in content
    assert "  model: gemma4:e2b\n" in content
    assert "servicebay-mcp:" in content
    assert "gatekeeper-mcp:" in content
    assert "servicebay_admin" not in content
    # cronjob load-bearing; kanban trimmed.
    assert "    - kanban\n" in content
    assert "    - cronjob\n" not in content


def test_profile_config_admin_carries_admin_mcp(pd):
    content = pd.render_profile_config_yaml(PROVIDER_URL, "gemma4:12b", ADMIN_SERVERS)
    assert "  model: gemma4:12b\n" in content
    assert "servicebay_admin:" in content
    assert f"Bearer {ADMIN}" in content
    assert "servicebay-mcp:" in content


# ── fake in-container filesystem (per-profile files are written via podman exec
#    because the profile dir is hermes-owned 0700 and unwritable host-side, #293)


class _FakeContainer:
    """Records what the post-deploy writes INTO the hermes container, so the
    provisioning logic can be asserted offline (no podman, no live container)."""

    def __init__(self):
        self.files: dict[str, str] = {}
        self.created: list[tuple[str, bool]] = []
        self.linked: list[tuple[str, str]] = []

    def write(self, path: str, content: str) -> bool:
        self.files[path] = content
        return True

    def read(self, path: str) -> str | None:
        return self.files.get(path)


@pytest.fixture
def fc(pd, monkeypatch):
    c = _FakeContainer()
    # The admin-soul SOUL.md is bind-mounted into the container; seed it so
    # provision_profiles can read the operator soul through the container.
    c.files["/opt/data/skills/admin-soul/SOUL.md"] = (
        TEMPLATES / "solilos" / "skills" / "admin-soul" / "SOUL.md"
    ).read_text(encoding="utf-8")
    monkeypatch.setattr(pd, "write_file_in_container", c.write)
    monkeypatch.setattr(pd, "read_file_in_container", c.read)
    monkeypatch.setattr(
        pd,
        "hermes_profile_create",
        lambda p, no_skills=False: c.created.append((p, no_skills)) or True,
    )
    monkeypatch.setattr(
        pd, "symlink_profile_skill", lambda p, s: c.linked.append((p, s)) or True
    )
    monkeypatch.setattr(pd, "admin_mcp_servers", lambda: ADMIN_SERVERS)
    return c


# ── per-profile SOUL.md, written via the container, operator edit preserved ──


def test_profile_soul_installed_then_preserved_when_edited(pd, fc):
    # The shipped soul is read THROUGH the container from its bind-mounted path
    # (host staging may omit skills/, box-verified #293).
    src_path = "/opt/data/skills/admin-soul/SOUL.md"
    shipped = (TEMPLATES / "solilos" / "skills" / "admin-soul" / "SOUL.md").read_text(
        encoding="utf-8"
    )
    fc.files[src_path] = shipped
    soul_path = "/opt/data/profiles/admin/SOUL.md"
    # The stock soul `hermes profile create` drops is overwritten (box-verified:
    # it is the "You are Hermes Agent … Nous Research" text, not STOCK_SOUL_MARKER).
    fc.files[soul_path] = (
        "You are Hermes Agent, an intelligent AI assistant created by Nous Research.\n"
    )
    assert pd.write_profile_soul("admin", src_path) is True
    assert fc.files[soul_path] == shipped
    # Idempotent: a redeploy with the same shipped soul does not rewrite.
    assert pd.write_profile_soul("admin", src_path) is False
    # A hand-customised soul (not stock, not Solilos/operator) is preserved.
    fc.files[soul_path] = "# Our House\n\nhand-tuned\n"
    assert pd.write_profile_soul("admin", src_path) is False
    assert fc.files[soul_path] == "# Our House\n\nhand-tuned\n"


# ── admin-gateway reboot-persistence boot hook (#299) ───────────────────────


def test_admin_boot_hook_forces_running_before_reconcile(pd, fc):
    # The hook is written VIA THE CONTAINER (the hermes data dir is 0700,
    # unwritable host-side, #299) to the cont-init mount source path.
    assert pd.write_admin_gateway_boot_hook() is True
    target = f"/opt/data/{pd.ADMIN_GATEWAY_BOOT_HOOK}"
    body = fc.files[target]
    # Sorts before the image's 02-reconcile-profiles so the reconciler reads the
    # forced state.
    assert pd.ADMIN_GATEWAY_BOOT_HOOK < "02-reconcile-profiles"
    # Targets the admin gateway's recorded state and forces it to running.
    assert "/opt/data/profiles/admin/gateway_state.json" in body
    assert '"gateway_state"] = "running"' in body
    # Writes as the hermes user so the file stays hermes-owned (gateway updates it).
    assert "s6-setuidgid hermes" in body
    # Idempotent on a second run with the same content.
    assert pd.write_admin_gateway_boot_hook() is False


# ── default-home .no-bundled-skills marker (#292), written via the container ─


def test_default_home_opts_out_and_removes_bundled(pd, monkeypatch):
    # The household persona is the DEFAULT profile served from /opt/data. We run
    # `hermes skills opt-out --remove` THROUGH the container so it both writes the
    # marker and deletes already-seeded bundled skills — the bare marker only
    # stopped re-seeding, leaving an older home loading the full catalog.
    import types

    calls: list[list[str]] = []

    def fake_run(args, **kw):
        calls.append(args)
        return types.SimpleNamespace(
            returncode=0, stdout="Removed 73; kept 2.", stderr=""
        )

    monkeypatch.setattr(pd.subprocess, "run", fake_run)
    assert pd.mark_default_home_no_bundled_skills() is True
    cmd = calls[0]
    assert cmd[:2] == ["podman", "exec"]
    assert pd.HERMES_CONTAINER in cmd
    assert "HERMES_HOME=/opt/data" in cmd
    assert cmd[-4:] == ["skills", "opt-out", "--remove", "--yes"]

    # A non-zero exit (e.g. container down) fails closed, not silently green.
    def fail_run(args, **kw):
        return types.SimpleNamespace(returncode=1, stdout="", stderr="not found")

    monkeypatch.setattr(pd.subprocess, "run", fail_run)
    assert pd.mark_default_home_no_bundled_skills() is False


# ── per-profile .env port (the only port lever that works, #293) ────────────


def test_profile_env_pins_api_server_port(pd, fc):
    assert pd.write_profile_env_port("admin", "8643") is True
    assert fc.files["/opt/data/profiles/admin/.env"] == "API_SERVER_PORT=8643\n"


def test_profile_config_has_no_api_server_block(pd):
    """The port is NOT in config.yaml (the container env overrides it); it lives
    in the per-profile .env instead."""
    content = pd.render_profile_config_yaml(PROVIDER_URL, "gemma4:12b", ADMIN_SERVERS)
    assert "api_server" not in content
    assert "platforms" not in content


# ── provision_profiles: ONLY the admin named profile, all via the container ──


def test_provision_builds_admin_only(pd, fc):
    pd.provision_profiles("/unused", PROVIDER_URL, admin_port="8643")

    # Only the admin profile is created — household is the DEFAULT profile — and
    # it is created --no-skills (lean, no bundled-catalog copy).
    assert fc.created == [("admin", True)]
    # admin-soul skills symlinked in from the shared bind mount.
    assert fc.linked == [("admin", "admin-soul")]

    adm_cfg = fc.files["/opt/data/profiles/admin/config.yaml"]
    assert "  model: gemma4:12b\n" in adm_cfg
    assert "servicebay_admin:" in adm_cfg
    assert "servicebay-mcp:" in adm_cfg
    assert "api_server" not in adm_cfg  # port via .env, not config
    # Port pinned via the per-profile .env (the only lever that works).
    assert fc.files["/opt/data/profiles/admin/.env"] == "API_SERVER_PORT=8643\n"
    # The admin profile gets the operator soul.
    assert "operator" in fc.files["/opt/data/profiles/admin/SOUL.md"].lower()


def test_provision_admin_env_overrides_model(pd, fc, monkeypatch):
    monkeypatch.setenv("ADMIN_PROFILE_MODEL", "gemma4:custom-adm")
    pd.provision_profiles("/unused", PROVIDER_URL, admin_port="8643")
    assert (
        "  model: gemma4:custom-adm\n"
        in fc.files["/opt/data/profiles/admin/config.yaml"]
    )

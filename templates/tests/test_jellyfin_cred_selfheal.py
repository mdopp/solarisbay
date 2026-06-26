"""Tests for the Jellyfin service-user credential self-heal (#626).

JELLYFIN_PASSWORD is a `noAutoGenerate` secret SB never stored, so every
template render zeroes it → the engine's AuthenticateByName as the read-only
`solaris` lldap user 401s → music down (happened live 2026-06-26). The
post-deploy converges the credential each deploy: persist a managed password
under DATA_DIR (mint once, reuse after), reset the lldap `solaris` user to it,
and stamp it into the deployed solaris.yml pod env. Idempotent + best-effort:
the same password each deploy is a no-op after the first, and any hiccup logs a
warning instead of raising. The lldap login + `lldap_set_password` exec are
monkeypatched so no live lldap/podman is needed.
"""

from __future__ import annotations

import importlib.util
import pathlib
import subprocess
import sys

import pytest

TEMPLATES = pathlib.Path(__file__).resolve().parents[1]


def _load(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def pd():
    return _load("solaris_pd_jelly_cred", TEMPLATES / "solaris" / "post-deploy.py")


def _pod_yml(value: str) -> str:
    return (
        "apiVersion: v1\n"
        "kind: Pod\n"
        "spec:\n"
        "  containers:\n"
        "  - name: chat\n"
        "    env:\n"
        "    - name: JELLYFIN_URL\n"
        '      value: "http://127.0.0.1:8096"\n'
        "    - name: JELLYFIN_PASSWORD\n"
        f"      value: {value}\n"
        "    - name: JELLYFIN_USERNAME\n"
        '      value: "solaris"\n'
    )


@pytest.fixture
def wired(pd, tmp_path, monkeypatch):
    """A pod yml with an empty JELLYFIN_PASSWORD, expanduser pinned at it, plus
    a stub lldap login (token) and a recorder for the lldap_set_password exec."""
    pod_yml = tmp_path / "solaris.yml"
    pod_yml.write_text(_pod_yml('""'), encoding="utf-8")
    monkeypatch.setattr(pd.os.path, "expanduser", lambda _p: str(pod_yml))
    monkeypatch.setattr(pd, "_lldap_admin_token", lambda: "lldap-admin-jwt")

    calls: list[list[str]] = []

    def _run(cmd, *a, **k):
        calls.append(list(cmd))
        return subprocess.CompletedProcess(
            cmd, 0, stdout="Successfully changed", stderr=""
        )

    monkeypatch.setattr(pd.subprocess, "run", _run)
    return tmp_path, pod_yml, calls


def _pw_value_line(pod_yml: pathlib.Path) -> str:
    src = pod_yml.read_text(encoding="utf-8")
    block = src.split("- name: JELLYFIN_PASSWORD\n", 1)[1]
    return block.splitlines()[0].strip()


def _persist_path(pd, data_dir: pathlib.Path) -> pathlib.Path:
    return data_dir / "solarisbay" / ".jellyfin-solaris-password"


def test_missing_pw_generates_persists_resets_and_applies(pd, wired):
    data_dir, pod_yml, calls = wired
    pw = pd.converge_jellyfin_credential(str(data_dir))
    assert pw
    # persisted (0600) for reuse on the next deploy
    persisted = _persist_path(pd, data_dir)
    assert persisted.read_text(encoding="utf-8").strip() == pw
    assert (persisted.stat().st_mode & 0o777) == 0o600
    # lldap reset called for the `solaris` user with the persisted value
    assert len(calls) == 1
    setpw = calls[0]
    assert setpw[:3] == ["podman", "exec", "auth-lldap"]
    assert "/app/lldap_set_password" in setpw
    assert setpw[setpw.index("-u") + 1] == "solaris"
    assert setpw[setpw.index("-p") + 1] == pw
    # engine env stamped to the same value
    assert _pw_value_line(pod_yml) == f'value: "{pw}"'


def test_existing_pw_is_reused_idempotent_no_churn(pd, wired):
    data_dir, pod_yml, calls = wired
    persisted = _persist_path(pd, data_dir)
    persisted.parent.mkdir(parents=True, exist_ok=True)
    persisted.write_text("preexisting-managed-pw\n", encoding="utf-8")
    pw = pd.converge_jellyfin_credential(str(data_dir))
    assert pw == "preexisting-managed-pw"
    # the persisted file is untouched (reused, not regenerated)
    assert persisted.read_text(encoding="utf-8").strip() == "preexisting-managed-pw"
    # lldap reset uses the persisted value
    assert calls[0][calls[0].index("-p") + 1] == "preexisting-managed-pw"
    assert _pw_value_line(pod_yml) == 'value: "preexisting-managed-pw"'


def test_lldap_reset_called_with_persisted_value(pd, wired, monkeypatch):
    data_dir, _pod_yml, calls = wired
    assert pd.reset_lldap_solaris_password("the-managed-pw") is True
    assert len(calls) == 1
    setpw = calls[0]
    assert setpw[setpw.index("-p") + 1] == "the-managed-pw"
    assert "--token" in setpw and setpw[setpw.index("--token") + 1] == "lldap-admin-jwt"


def test_best_effort_lldap_login_missing_does_not_raise(pd, wired, monkeypatch):
    data_dir, pod_yml, calls = wired
    # No lldap admin token resolvable (container env empty) → reset is skipped,
    # but the converge still persists + stamps the engine env and never raises.
    monkeypatch.setattr(pd, "_lldap_admin_token", lambda: "")
    pw = pd.converge_jellyfin_credential(str(data_dir))
    assert pw
    assert calls == []  # lldap_set_password never invoked
    assert _pw_value_line(pod_yml) == f'value: "{pw}"'


def test_best_effort_lldap_exec_failure_does_not_raise(pd, wired, monkeypatch):
    data_dir, _pod_yml, _calls = wired

    def _boom(cmd, *a, **k):
        raise OSError("podman not found")

    monkeypatch.setattr(pd.subprocess, "run", _boom)
    # A failing exec must not propagate — best-effort.
    assert pd.reset_lldap_solaris_password("x") is False


def test_apply_returns_false_when_no_pw_env_entry(pd, wired):
    data_dir, pod_yml, _calls = wired
    pod_yml.write_text(
        "apiVersion: v1\nkind: Pod\nspec:\n  containers:\n  - name: chat\n",
        encoding="utf-8",
    )
    assert pd.apply_jellyfin_password_to_engine("pw") is False


def test_apply_returns_false_when_pod_yml_missing(pd, wired):
    data_dir, pod_yml, _calls = wired
    pod_yml.unlink()
    assert pd.apply_jellyfin_password_to_engine("pw") is False


def test_no_password_logged(pd, wired, capsys):
    data_dir, _pod_yml, _calls = wired
    pw = pd.converge_jellyfin_credential(str(data_dir))
    out = capsys.readouterr().out
    assert pw and pw not in out

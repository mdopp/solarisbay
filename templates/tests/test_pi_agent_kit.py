"""The ServiceBay agent kit inside the pi-web pod (#1398 slice A).

ServiceBay delivers the assist catalog, its agent CLI and one `AGENTS.md` to a
path on the box; this pod mounts them and reshapes them into what Pi actually
reads. Every failure here is quiet:

* **A mount of the checkout root** instead of its three subdirectories would put
  ServiceBay's own `CLAUDE.md` and README into a container whose job is to work
  on a *different* repository — a second set of instructions the model would
  follow, and nothing about a running pod would say so.
* **A skill without a description** is skipped by Pi with a warning nobody
  reads, so the assist it was generated from is simply never offered.
* **A token file resolved to the pod's** where the project has its own would
  make per-project revocation meaningless — and both calls succeed, so the only
  symptom is a credential that outlives the project.
* **A wrapper that put the token in argv** would leak it to every process on the
  container through `/proc/<pid>/cmdline`, which is exactly why ServiceBay's CLI
  has no `--token` flag to begin with.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys

import pytest
import yaml

TEMPLATES = pathlib.Path(__file__).resolve().parents[1]
PI_WEB = TEMPLATES / "pi-web"
ROOT = TEMPLATES.parent
KIT = ROOT / "pi-web" / "pi_agent_kit.py"
WRAPPER = ROOT / "pi-web" / "pi_servicebay.py"

MOUNT_ROOT = "/opt/servicebay"
# What ServiceBay delivers to on the box, written out in the pod spec as a
# literal — not a template variable (#1403) and not `{{DATA_DIR}}` (#1404):
# that macro is scoped per service (box-verified as `/mnt/data/stacks/pi-web`
# for this pod, not the flat `/mnt/data` its name suggests), while this
# checkout is ServiceBay's own shared delivery target, one copy for every
# consumer regardless of which service asks for it.
CHECKOUT = "/mnt/data/servicebay/agent-kit/checkout"
KIT_MOUNTS = {
    f"{MOUNT_ROOT}/agent-cli": "agent-kit-cli",
    f"{MOUNT_ROOT}/agent-docs": "agent-kit-docs",
    f"{MOUNT_ROOT}/assists": "agent-kit-assists",
}

# One real catalog entry's shape: a `---` head with flat `key: value` lines, the
# value sometimes quoted, always a `whenToUse` written for the reader's
# situation. Generated skills stand or fall on those two fields.
ASSIST = """---
title: "ADR 0007 — App containers move off `hostNetwork`"
whenToUse: "You are choosing a network mode for a template: whether a pod may set hostNetwork, and which address a cross-service reference must use."
kind: adr
tags: [adr, decision, network]
---
# ADR 0007

## Decision

App templates drop `hostNetwork` and publish a `hostPort` instead.
"""


# The delivered catalog does not live in this repo — ServiceBay delivers it to
# the box — so the frontmatter classes the generator has to tolerate are pinned
# here verbatim from mdopp/servicebay: a colon inside the value, embedded
# quotes, and a backslash. Each pair is the raw right-hand side, quoted exactly
# as the assist writes it, so `parse_frontmatter` reads here what it reads there.
CATALOG = {
    "adr-0009-service-tokens-and-trust": (
        '"ADR 0009 — Tokens & trust between services"',
        '"You are giving one component authority to act on another: minting or scoping an API token, adding a service-to-service call, an exec allow-list entry, a pairing or reconnect bootstrap, or deciding what must survive a wipe-config reinstall — this names the trust layer each credential belongs in and why nothing gets ambient authority."',
    ),
    "adr-0012-repair-is-reconciliation-not-reinstallation": (
        '"ADR 0012 — Repair is reconciliation, not reinstallation — with hard guardrails against a reconciler hell"',
        '"A drifted config, credential or registration is about to be \\"fixed\\" by reinstalling or redeploying, or you are adding a reconciler, a self-heal action, a desired-state loop or a background repair timer — this sets the guardrails: idempotent, diff-first and visible, explicitly triggered, and no controller fabric."',
    ),
    "adr-0015-template-deletions-need-a-delivered-files-manifest": (
        "ADR 0015 — A template deletes only what it demonstrably delivered; the record is a delivered-files manifest",
        'You are about to make a deploy, sync or reconcile pass remove files from a box — a mirroring sync, an rsync --delete, a "clean up what the template no longer ships", a stale-file cleanup — and need the rule for what may be deleted; or you are wondering why a file removed from a template\'s source tree is still on the node, or why an existing install did not converge after the fix.',
    ),
    "footgun-journal-is-a-buffer-not-an-archive": (
        "The systemd journal is a rotating buffer, not an archive — a service that must reconstruct its own past writes its own log",
        'You are about to rely on `journalctl` as the record of something that happened more than a few days ago (a failed run, an audit trail, "what happened to my request last week"), or you are sizing/quoting a journal retention window before you\'ve checked the actual knobs.',
    ),
    "recipe-walk-a-human-through-a-manual-acceptance": (
        "Walking a human through a manual acceptance",
        'A person has to confirm something on a device, a phone or an installation that you cannot check yourself — a release check, a device test, an on-site observation. Read this before you write "please check whether it works".',
    ),
    "footgun-vaultwarden-personal-vault-write": (
        "You cannot write into a personal Vaultwarden/Bitwarden vault with an API key",
        'You are about to build automation that pushes credentials into Vaultwarden (or Bitwarden) — a sync job, an install hook, a "store this password for the user" feature.',
    ),
}


def assist(title: str, when: str) -> str:
    """One catalog entry in the shape the generator reads it off the mount."""
    return f"---\ntitle: {title}\nwhenToUse: {when}\nkind: adr\ntags: [adr, decision]\n---\n\n# body\n"


def frontmatter(skill: str) -> dict:
    """The generated `SKILL.md` head as a real YAML parser sees it — which is how
    Pi sees it, and the whole point of #1410."""
    lines = skill.splitlines()
    assert lines[0] == "---"
    return yaml.safe_load("\n".join(lines[1 : lines.index("---", 1)]))


def _load(name: str, path: pathlib.Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def kit():
    return _load("pi_agent_kit", KIT)


@pytest.fixture(scope="module")
def wrapper():
    return _load("pi_servicebay", WRAPPER)


@pytest.fixture(scope="module")
def variables() -> dict:
    return json.loads((PI_WEB / "variables.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def pod(variables) -> dict:
    rendered = (PI_WEB / "template.yml").read_text(encoding="utf-8")
    for key, spec in variables.items():
        rendered = rendered.replace("{{" + key + "}}", str(spec.get("default", "")))
    for key, value in (("DATA_DIR", "/mnt/data"), ("PUBLIC_DOMAIN", "example")):
        rendered = rendered.replace("{{" + key + "}}", value)
    return yaml.safe_load(rendered)


def containers(pod: dict, name: str) -> dict:
    for group in ("initContainers", "containers"):
        for entry in pod["spec"].get(group, []):
            if entry["name"] == name:
                return entry
    raise AssertionError(f"no container {name}")


# ── the mount ───────────────────────────────────────────────────────────────


def test_the_kit_is_mounted_by_subdirectory_and_never_by_its_root(pod):
    volumes = {v["name"]: v for v in pod["spec"]["volumes"]}
    for mount_path, volume in KIT_MOUNTS.items():
        expected = f"{CHECKOUT}/{mount_path.rsplit('/', 1)[-1]}"
        assert volumes[volume]["hostPath"]["path"] == expected
    assert CHECKOUT not in {v["hostPath"]["path"] for v in volumes.values()}


def test_every_container_that_runs_an_agent_sees_the_kit_read_only(pod):
    for name in ("pi-web-agent-kit", "sessiond", "web", "autoloop"):
        mounts = {
            m["mountPath"]: m for m in containers(pod, name).get("volumeMounts", [])
        }
        for mount_path, volume in KIT_MOUNTS.items():
            assert mounts[mount_path]["name"] == volume, (name, mount_path)
            assert mounts[mount_path]["readOnly"] is True, (name, mount_path)


def test_the_kit_path_is_named_for_the_model_not_hard_coded(pod):
    """The shipped AGENTS.md tells the model to reach the catalog and the CLI
    through `$SERVICEBAY_AGENT_KIT`; a session whose shell lacks it would follow
    the handbook to a path that expands to nothing."""
    for name in ("sessiond", "web", "autoloop"):
        env = {e["name"]: e["value"] for e in containers(pod, name).get("env", [])}
        assert env["SERVICEBAY_AGENT_KIT"] == MOUNT_ROOT


def test_the_generator_runs_on_every_start_as_an_init_step(pod):
    """Not once at install: ServiceBay refreshes the checkout hourly, so the next
    start is what carries a changed assist into the sessions."""
    step = containers(pod, "pi-web-agent-kit")
    assert step["args"] == ["pi-web-agent-kit"]
    assert step in pod["spec"]["initContainers"]


def test_a_box_without_the_kit_still_starts_pi_web(pod):
    """An older ServiceBay has delivered nothing yet. `Directory` would fail the
    whole pod over a missing directory; the empty mount only costs the skills."""
    volumes = {v["name"]: v for v in pod["spec"]["volumes"]}
    for volume in KIT_MOUNTS.values():
        assert volumes[volume]["hostPath"]["type"] == "DirectoryOrCreate"


def test_the_kit_path_is_fixed_and_never_an_installer_variable(variables):
    """#1403: it was `PI_WEB_AGENT_KIT_DIR` for one release, and a plain upgrade
    of an installed service rendered it **empty** — ServiceBay does not apply the
    default of a newly added variable to an existing service (servicebay#2913).
    The hostPath became `/agent-cli`, the pod crash-looped on a read-only root,
    and the install job still said `done`."""
    assert "PI_WEB_AGENT_KIT_DIR" not in variables
    template = (PI_WEB / "template.yml").read_text(encoding="utf-8")
    assert "PI_WEB_AGENT_KIT_DIR" not in template
    # Not `{{DATA_DIR}}` either (#1404 tried that): box-verified against #1404
    # as scoped per service (`/mnt/data/stacks/pi-web`, not flat `/mnt/data`),
    # while this checkout is ServiceBay's own shared delivery target, the same
    # copy for every consumer regardless of which service asks for it. (Other
    # pi-web volumes legitimately keep `{{DATA_DIR}}` — this checks only the
    # checkout's own three lines.)
    for mount_path in KIT_MOUNTS:
        leaf = mount_path.rsplit("/", 1)[-1]
        assert f"path: {CHECKOUT}/{leaf}" in template


# ── assists as Pi skills ────────────────────────────────────────────────────


def test_an_assist_becomes_a_skill_pi_will_load(kit, tmp_path):
    (tmp_path / "assists").mkdir()
    (tmp_path / "assists" / "adr-0007-network.md").write_text(ASSIST, encoding="utf-8")
    report = kit.generate_skills(str(tmp_path / "assists"), str(tmp_path / "skills"))

    skill = tmp_path / "skills" / "servicebay" / "adr-0007-network" / "SKILL.md"
    text = skill.read_text(encoding="utf-8")
    assert report["skills"] == 1 and report["written"] == 1
    assert text.startswith("---\nname: adr-0007-network\ndescription: ")
    # The description is what stays in Pi's context, so it has to carry the
    # `whenToUse` line that says when to open the skill at all.
    assert "choosing a network mode" in text.split("---")[1]
    assert "App templates drop `hostNetwork`" in text


def test_regenerating_an_unchanged_catalog_writes_nothing(kit, tmp_path):
    (tmp_path / "assists").mkdir()
    (tmp_path / "assists" / "adr-0007-network.md").write_text(ASSIST, encoding="utf-8")
    kit.generate_skills(str(tmp_path / "assists"), str(tmp_path / "skills"))
    again = kit.generate_skills(str(tmp_path / "assists"), str(tmp_path / "skills"))
    assert again["written"] == 0 and again["pruned"] == []


def test_a_retired_assist_stops_being_offered(kit, tmp_path):
    (tmp_path / "assists").mkdir()
    (tmp_path / "assists" / "adr-0007-network.md").write_text(ASSIST, encoding="utf-8")
    (tmp_path / "assists" / "gone.md").write_text(ASSIST, encoding="utf-8")
    kit.generate_skills(str(tmp_path / "assists"), str(tmp_path / "skills"))

    (tmp_path / "assists" / "gone.md").unlink()
    report = kit.generate_skills(str(tmp_path / "assists"), str(tmp_path / "skills"))
    assert report["pruned"] == ["gone"]
    assert not (tmp_path / "skills" / "servicebay" / "gone").exists()


def test_a_failed_delivery_does_not_empty_the_skills(kit, tmp_path):
    """An empty mount is ServiceBay's outage to report. Wiping the skills over it
    would turn one loud failure into a second, silent one here."""
    (tmp_path / "assists").mkdir()
    (tmp_path / "assists" / "adr-0007-network.md").write_text(ASSIST, encoding="utf-8")
    kit.generate_skills(str(tmp_path / "assists"), str(tmp_path / "skills"))
    (tmp_path / "assists" / "adr-0007-network.md").unlink()

    report = kit.generate_skills(str(tmp_path / "assists"), str(tmp_path / "skills"))
    assert report["catalog"] == "missing"
    assert (tmp_path / "skills" / "servicebay" / "adr-0007-network").is_dir()


def test_an_assist_without_a_description_is_not_generated(kit):
    assert kit.render_skill("bare", "no frontmatter at all\n") is None


def test_generated_names_and_descriptions_stay_inside_pis_limits(kit):
    long_id = "adr-0099-" + "x" * 200
    name = kit.skill_name(long_id)
    assert len(name) <= kit.NAME_MAX and not name.endswith("-")

    fields = {"title": "T" * 900, "whenToUse": "W" * 900}
    assert len(kit.skill_description(fields)) <= kit.DESCRIPTION_MAX


def test_every_catalog_frontmatter_renders_as_parseable_yaml(kit, tmp_path):
    """Every assist has to survive the round trip, not two hand-picked ones.

    17 of the catalog's 55 assists rendered a head no YAML parser accepts
    (#1410) and were silently never loaded: 8 over a `: ` in the value, 10 over
    an embedded quote, one of those with a backslash too. Wrapping the value in
    quotes fixes the first class and breaks the other two, so what is asserted
    here is the parse and the round-trip, never the punctuation.
    """
    (tmp_path / "assists").mkdir()
    for assist_id, (title, when) in CATALOG.items():
        (tmp_path / "assists" / f"{assist_id}.md").write_text(
            assist(title, when), encoding="utf-8"
        )
    report = kit.generate_skills(str(tmp_path / "assists"), str(tmp_path / "skills"))
    assert report["skills"] == len(CATALOG)

    for assist_id, (title, when) in CATALOG.items():
        skill = tmp_path / "skills" / "servicebay" / assist_id / "SKILL.md"
        head = frontmatter(skill.read_text(encoding="utf-8"))
        fields, _ = kit.parse_frontmatter(assist(title, when))
        assert head["name"] == assist_id
        assert head["description"] == kit.skill_description(fields)


def test_a_clipped_description_stays_valid_yaml(kit):
    """The clip runs on the description, so it has to run before the escaping: a
    cut through an escape sequence gives the same unloadable skill by another
    route. The tail puts a backslash, a quote or a `: ` on every candidate cut
    point."""
    tail = '\\"x: ' * 12
    for pad in range(12):
        title = "T" * (kit.DESCRIPTION_MAX - 4 - pad)
        head = frontmatter(kit.render_skill("clipped", assist(title, tail)))
        expected = kit.skill_description({"title": title, "whenToUse": tail})
        assert head["description"] == expected
        assert expected.endswith("…") and len(expected) <= kit.DESCRIPTION_MAX


def test_the_generator_writes_nothing_into_the_read_only_kit(kit):
    """The mount is read-only and refreshed hourly; a write there would fail on
    the box and reach nobody if it did not."""
    source = KIT.read_text(encoding="utf-8")
    assert "assists_dir" in source
    for line in source.splitlines():
        if "open(" in line and '"w"' in line:
            assert "assists" not in line and "docs_dir" not in line, line


# ── the global AGENTS.md ────────────────────────────────────────────────────


def test_the_global_context_file_is_the_prelude_then_the_shipped_handbook(
    kit, tmp_path
):
    docs = tmp_path / "agent-docs"
    docs.mkdir()
    (docs / "AGENTS.md").write_text("# Working on a ServiceBay box\n", encoding="utf-8")
    report = kit.install_agents_md(str(docs), str(tmp_path / "agent"))

    text = (tmp_path / "agent" / "AGENTS.md").read_text(encoding="utf-8")
    assert report["changed"] and report["shipped"]
    assert text.index("PI WEB container") < text.index("Working on a ServiceBay box")


def test_the_prelude_says_the_three_things_only_this_box_knows(kit):
    """It is deliberately short: everything general is in the shipped file, and a
    second copy of that here is the drift ADR 0014 exists to prevent."""
    assert "/workspace" in kit.PRELUDE
    assert "`servicebay` is on `$PATH`" in kit.PRELUDE
    assert "gate" in kit.PRELUDE
    assert len(kit.PRELUDE.splitlines()) < 30


def test_the_handbook_is_never_shortened_into_the_prelude(kit):
    shipped = "# Working on a ServiceBay box\n\nEvery word of it.\n"
    assert "Every word of it." in kit.render_agents_md(shipped)


def test_an_unmounted_kit_logs_one_line_and_does_not_fail_the_pod(
    kit, tmp_path, capsys
):
    """The init container's exit code is the pod's life. A box whose ServiceBay
    delivers no kit yet must cost the skills, not `pi.<domain>` (#1403)."""
    assert (
        kit.main(
            ["--kit", str(tmp_path / "nothing"), "--agent-dir", str(tmp_path / "agent")]
        )
        == 0
    )
    err = capsys.readouterr().err.strip().splitlines()
    assert len(err) == 1
    assert "no agent kit mounted at" in err[0]


def test_a_missing_handbook_still_leaves_the_box_prelude(kit, tmp_path):
    report = kit.install_agents_md(str(tmp_path / "nowhere"), str(tmp_path / "agent"))
    assert report["shipped"] is False
    assert "PI WEB container" in (tmp_path / "agent" / "AGENTS.md").read_text(
        encoding="utf-8"
    )


# ── `servicebay` on PATH ────────────────────────────────────────────────────


def test_the_wrapper_runs_the_delivered_cli(wrapper):
    assert wrapper.cli_path(MOUNT_ROOT) == f"{MOUNT_ROOT}/agent-cli/servicebay.mjs"


def test_a_session_inside_a_project_reads_the_box_as_that_project(wrapper):
    assert wrapper.project_for_cwd("/workspace", "/workspace/solarisbay/src") == (
        "solarisbay"
    )
    assert wrapper.project_for_cwd("/workspace", "/workspace") == ""
    assert wrapper.project_for_cwd("/workspace", "/data/pi-web") == ""


def test_the_projects_own_token_wins_over_the_pods(wrapper):
    cfg = wrapper.config_from_env({})
    project_token = "/data/servicebay/projects/solarisbay.token"
    assert (
        wrapper.token_file(cfg, "/workspace/solarisbay", lambda p: p == project_token)
        == project_token
    )


def test_a_hand_cloned_checkout_falls_back_to_the_pod_token(wrapper):
    """Read access at the pod's own read-only scope beats an error a session
    cannot act on — nothing here widens a scope, both tokens are `read`."""
    cfg = wrapper.config_from_env({})
    chosen = wrapper.token_file(cfg, "/workspace/hand-cloned", lambda p: False)
    assert chosen == wrapper.DEFAULT_PARENT_TOKEN_FILE


def test_the_wrapper_names_the_token_file_and_carries_no_token(wrapper):
    env = wrapper.child_env({"SERVICEBAY_MCP_TOKEN": "sb_dead_beef"}, "/tmp/tok")
    assert env["SERVICEBAY_MCP_TOKEN_FILE"] == "/tmp/tok"
    assert "SERVICEBAY_MCP_TOKEN" not in env


def test_the_wrapper_passes_the_path_and_never_the_secret(
    wrapper, tmp_path, monkeypatch
):
    """ServiceBay's CLI has no `--token` flag because `/proc/<pid>/cmdline` is
    world-readable. Neither has this: the argv it builds is the script and the
    session's own words, and the token stays a path in the environment."""
    kit = tmp_path / "kit"
    (kit / "agent-cli").mkdir(parents=True)
    (kit / "agent-cli" / "servicebay.mjs").write_text("//\n", encoding="utf-8")
    token = tmp_path / "parent-token"
    token.write_text("sb_00000000_SECRET\n", encoding="utf-8")

    seen = {}
    monkeypatch.setattr(
        wrapper.os,
        "execvpe",
        lambda file, argv, env: seen.update(file=file, argv=argv, env=env),
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SERVICEBAY_AGENT_KIT", str(kit))
    monkeypatch.setenv("PI_WEB_SB_TOKEN_FILE", str(token))
    wrapper.main(["assist", "adr-0007-x"])

    assert seen["file"] == "node"
    assert seen["argv"] == [
        "node",
        str(kit / "agent-cli" / "servicebay.mjs"),
        "assist",
        "adr-0007-x",
    ]
    assert seen["env"]["SERVICEBAY_MCP_TOKEN_FILE"] == str(token)
    assert "SECRET" not in " ".join(seen["argv"])


def test_the_image_puts_the_cli_and_the_generator_where_the_pod_calls_them():
    dockerfile = (ROOT / "pi-web" / "Dockerfile").read_text(encoding="utf-8")
    assert "pi_servicebay.py /usr/local/bin/servicebay" in dockerfile
    assert "pi_agent_kit.py /usr/local/bin/pi-web-agent-kit" in dockerfile

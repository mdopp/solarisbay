"""The Pi extension that keeps PI WEB's model list from going stale (#1435).

Measured on the box on 19.9.: `models.json` was dated 13.09. 20:13, listed three
of the four presets, and `gemma-4-12b` had been unpickable since #1431 started
serving it. Pi's documented hourly catalog refresh existed the whole time and
never ran for us — in `@earendil-works/pi-ai/dist/models.js`, `createProvider`
sets `refreshModels: fetchModels ? … : undefined`, and a provider that arrives
in CONFIG form (which is what a models.json entry becomes) brings no
`fetchModels`.

Three things here have a wrong answer that looks like a working install:

* **Native form.** `pi.registerProvider("id", config)` and
  `pi.registerProvider(provider)` differ by one argument and only the second may
  carry a fetch. The config form would install cleanly, log nothing, and leave
  the list exactly as frozen as it is today.
* **The merge.** Once a fetch exists, a fetched entry REPLACES the hand-written
  one of the same id. A `models` array left behind in models.json wins over the
  live catalog — so the file would go on being the source of truth while looking
  retired.
* **The install.** It runs on every pod start. A step that appended, or that
  left a previous version's file behind, would drift silently: Pi loads every
  file in the extensions directory, so a stale copy is a second provider
  registration, not a dead file.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import shutil
import subprocess

import pytest
import yaml

TEMPLATES = pathlib.Path(__file__).resolve().parents[1]
PI_WEB = TEMPLATES / "pi-web"
ROOT = TEMPLATES.parent
EXTENSION = ROOT / "pi-web" / "extensions" / "solaris-llama.js"


@pytest.fixture(scope="module")
def pod() -> dict:
    text = (PI_WEB / "template.yml").read_text(encoding="utf-8")
    variables = json.loads((PI_WEB / "variables.json").read_text(encoding="utf-8"))
    for name, spec in variables.items():
        text = text.replace("{{%s}}" % name, str(spec.get("default", "x")))
    text = text.replace("{{PUBLIC_DOMAIN}}", "example.test")
    text = text.replace("{{DATA_DIR}}", "/mnt/data/stacks")
    return yaml.safe_load(text)


@pytest.fixture(scope="module")
def extension_text() -> str:
    return EXTENSION.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def extension_code(extension_text) -> str:
    """The extension with its block comments removed — the prose has to name the
    config form in order to explain why it is not used. Line comments are left
    in: stripping them would also take the `//` out of every URL."""
    return re.sub(r"/\*.*?\*/", "", extension_text, flags=re.S)


def init_container(pod: dict, name: str) -> dict:
    return next(c for c in pod["spec"]["initContainers"] if c["name"] == name)


# ── the registration ─────────────────────────────────────────────────────────


def test_the_provider_is_registered_in_native_form_with_a_fetch(extension_code):
    """The whole point of the file. The config form takes a name and a config
    object and cannot carry `refreshModels`; the native form takes one provider
    and can. PI WEB captures both at daemon start, so only this difference
    decides whether the hourly refresh has anything to call."""
    assert "pi.registerProvider({" in extension_code
    assert 'pi.registerProvider("' not in extension_code
    assert "pi.registerProvider(PROVIDER_ID" not in extension_code
    assert "refreshModels: async (context)" in extension_code
    assert "fetchCatalog(context.signal)" in extension_code


def test_the_catalog_is_fetched_from_the_pod_gate(extension_text):
    """Not from the host's LLAMA_PORT: this pod has its own netns (ADR 0007),
    and the gate beside it is what carries the names and the windows. And never
    the Engine's lease API, which this pod may not address at all."""
    assert 'DEFAULT_GATE_URL = "http://127.0.0.1:11437/v1"' in extension_text
    assert "host.containers.internal" not in extension_text
    assert "8787" not in extension_text


def test_a_gate_that_is_not_up_yet_is_not_a_failed_extension(extension_code):
    """An extension that throws in its factory is dropped, and with it the
    provider — PI WEB would then have no model at all because the gate container
    happened to be a second behind."""
    assert "catch (error)" in extension_code
    assert "let models = [];" in extension_code


def test_the_provider_id_is_the_one_models_json_seeds(extension_code):
    """Two ids would be two providers: the seed's connection would stay
    modelless and the fetched list would arrive under a name nothing points at."""
    pd_text = (PI_WEB / "post-deploy.py").read_text(encoding="utf-8")
    assert 'PROVIDER_ID = "solaris-llama"' in pd_text
    assert 'const PROVIDER_ID = "solaris-llama";' in extension_code


# ── the merge pi-ai performs, applied to what we actually ship ───────────────


def merged(baseline: list[dict], fetched: list[dict]) -> list[dict]:
    """`createProvider`'s own merge, transcribed from `dist/models.js`.

    Same id: the fetched entry REPLACES the baseline one. New id: appended.
    """
    result = list(baseline)
    for model in fetched:
        index = next(
            (i for i, entry in enumerate(result) if entry["id"] == model["id"]), -1
        )
        if index >= 0:
            result[index] = model
        else:
            result.append(model)
    return result


def test_the_hand_written_names_survive_a_refresh():
    """The catalog the gate serves is what the merge keeps, because models.json
    contributes no entry to overwrite it with. If a `models` array ever comes
    back into that file, this fails — which is the point: it would win over the
    live list and be the stale source of truth again."""
    import importlib.util
    import sys

    def load(name: str, path: pathlib.Path):
        spec = importlib.util.spec_from_file_location(name, path)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module

    gate = load("pi_model_gate_catalog", ROOT / "pi-web" / "pi_model_gate.py")
    pd = load("pi_web_pd_catalog", PI_WEB / "post-deploy.py")

    provider = pd.models_document("11437")["providers"][pd.PROVIDER_ID]
    baseline = [
        {"id": entry["id"], "name": entry["name"]}
        for entry in provider.get("models", [])
    ]
    fetched = [
        {"id": preset, "name": gate.pi_model(preset)["name"]} for preset in gate.PRESETS
    ]
    names = {entry["id"]: entry["name"] for entry in merged(baseline, fetched)}
    assert names["qwen3.8-27b"] == "Qwen 3.8 27B (Programmieren)"
    assert names["qwen3.6-35b-a3b"] == "Qwen 3.6 35B-A3B (Denken)"
    assert names["gemma-4-e4b"] == "Gemma 4 E4B (Haushaltsmodell)"
    assert names["gemma-4-12b"] == "Gemma 4 12B (Haushalt + Denken)"


# ── the install ──────────────────────────────────────────────────────────────


def test_the_extension_ships_in_the_image(pod):
    """It is written against the Pi extension API of the pinned PI_WEB_VERSION,
    so it moves with the image and not with the template's asset tree."""
    dockerfile = (ROOT / "pi-web" / "Dockerfile").read_text(encoding="utf-8")
    assert "COPY extensions /opt/solaris/pi-extensions" in dockerfile


def test_the_install_runs_before_sessiond_starts(pod):
    """Providers are captured when the session daemon starts and frozen for its
    lifetime — an extension installed afterwards is a logged no-op. An init
    container is what makes an ordinary deploy the one restart this needs."""
    names = [c["name"] for c in pod["spec"]["initContainers"]]
    assert "pi-web-extensions" in names
    assert "sessiond" in [c["name"] for c in pod["spec"]["containers"]]


def run_install(pod: dict, root: pathlib.Path, payload: str) -> None:
    """Run the real `pi-web-extensions` step against a fake image and volume."""
    source = root / "opt" / "pi-extensions"
    source.mkdir(parents=True, exist_ok=True)
    (source / "solaris-llama.js").write_text(payload, encoding="utf-8")

    stub_bin = root / "bin"
    stub_bin.mkdir(exist_ok=True)
    (stub_bin / "pi").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (stub_bin / "pi").chmod(0o755)

    script = init_container(pod, "pi-web-extensions")["args"][-1].replace(
        "/opt/solaris/pi-extensions", str(source)
    )
    subprocess.run(
        ["sh", "-c", script],
        check=True,
        env={
            **os.environ,
            "PATH": f"{stub_bin}:{os.environ['PATH']}",
            "HOME": str(root / "home"),
            "PI_CODING_AGENT_DIR": str(root / "pi-agent"),
        },
    )


def test_the_install_is_idempotent(pod, tmp_path):
    """It runs on every pod start. Running it twice has to leave exactly one
    copy of exactly the shipped file — Pi loads every file in that directory, so
    a leftover is a second provider registration and not a dead file."""
    installed = tmp_path / "pi-agent" / "extensions"
    run_install(pod, tmp_path, "// v1\n")
    first = sorted(p.name for p in installed.iterdir())
    run_install(pod, tmp_path, "// v1\n")
    assert sorted(p.name for p in installed.iterdir()) == first == ["solaris-llama.js"]
    assert (installed / "solaris-llama.js").read_text(encoding="utf-8") == "// v1\n"


def test_an_upgrade_replaces_the_previous_version(pod, tmp_path):
    """`rm -rf` before `cp -a`: a version left behind would keep registering
    the provider it shipped with, and the last registration wins."""
    installed = tmp_path / "pi-agent" / "extensions"
    run_install(pod, tmp_path, "// v1\n")
    run_install(pod, tmp_path, "// v2\n")
    assert (installed / "solaris-llama.js").read_text(encoding="utf-8") == "// v2\n"
    assert sorted(p.name for p in installed.iterdir()) == ["solaris-llama.js"]


def test_a_file_the_operator_added_is_left_alone(pod, tmp_path):
    """The step owns the names it ships, not the directory: `pi install` puts
    third-party extensions in the same place (#1423)."""
    installed = tmp_path / "pi-agent" / "extensions"
    installed.mkdir(parents=True)
    (installed / "eigenes.js").write_text("// handmade\n", encoding="utf-8")
    run_install(pod, tmp_path, "// v1\n")
    assert (installed / "eigenes.js").read_text(encoding="utf-8") == "// handmade\n"


def test_the_third_party_install_still_runs_after_ours(pod):
    """`pi-subagents` (#1423) is the other half of this step and must not be
    lost to the addition — and its failure must still not fail the pod."""
    script = init_container(pod, "pi-web-extensions")["args"][-1]
    assert "pi install npm:pi-subagents" in script
    assert "install failed, keeping what the volume holds" in script


def test_sh_is_enough_to_run_the_step(pod):
    """The image's shell for an `sh -c` step is dash, not bash — a bashism here
    would fail the init container and take the whole pod with it."""
    assert shutil.which("sh")
    assert init_container(pod, "pi-web-extensions")["args"][:2] == ["sh", "-c"]

"""The GPU lease (#1320) and its one named mode (#1435): what `acquire` stops,
what it allows, and what `release` gives back.

The unit list is the load-bearing part — a missing `llama.service` leaves
Solaris' own 3.9 GB server loaded and the Qwen run then OOMs on a card measured
full at 15.0 of 16.4 GB. The ordering matters just as much: the lease file is
written before the stop and removed after the model answers again, so there is
no moment when the card is gone and nothing knows it.

`erweitert` is the softer shape: since #1416 llama-server runs in router mode
and serves every preset, so the mode stops nothing on the llama side at all —
it sets the environment (voice device, background GPU jobs, embeddings server)
and writes the presets it allows, which since #1435 is all of them. `foundry`,
`thinking` and `coding` are the names it used to have and are still accepted;
they decide only which preset the holder is told it will be answered by, and a
lease file left on disk under one of them migrates on read.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
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
    return _load("llama_pd_lease", TEMPLATES / "llama" / "post-deploy.py")


@pytest.fixture(autouse=True)
def no_box(pd, monkeypatch):
    """Nothing here may reach the box: `systemd-run`, `daemon-reload` and the
    timer stop all go through subprocess. Returns the recorded argv list."""
    calls: list[list[str]] = []

    class _Done:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(
        pd.subprocess, "run", lambda argv, **kw: calls.append(list(argv)) or _Done()
    )
    return calls


@pytest.fixture
def systemctl_calls(pd, monkeypatch):
    """Record `(verb, units)` instead of touching the box's units."""
    calls: list[tuple[str, tuple[str, ...]]] = []
    monkeypatch.setattr(
        pd, "systemctl", lambda verb, units: bool(calls.append((verb, units))) or True
    )
    return calls


def _lease(tmp_path, pd) -> pathlib.Path:
    return tmp_path / "solarisbay" / pd.LEASE_FILE


def test_leased_units_cover_voice_embeddings_and_solaris_own_server(pd):
    assert set(pd.LEASED_UNITS) == {
        "llama-embed.service",
        "solaris-whisper.service",
        "solaris-whisper-batch.service",
        "solaris-tts.service",
        "solaris-wakeword-trainer.service",
        "llama.service",
    }


def test_acquire_stops_exactly_the_leased_units(pd, tmp_path, systemctl_calls):
    assert pd.lease_acquire(str(tmp_path), "coder") == 0
    assert systemctl_calls == [("stop", pd.LEASED_UNITS)]
    written = json.loads(_lease(tmp_path, pd).read_text())
    assert written["holder"] == "coder"
    assert written["since"] > 0


def test_acquire_claims_before_it_stops(pd, tmp_path, monkeypatch):
    held_when_stopping: list[bool] = []
    monkeypatch.setattr(
        pd,
        "systemctl",
        lambda verb, units: (
            bool(held_when_stopping.append(_lease(tmp_path, pd).exists())) or True
        ),
    )
    assert pd.lease_acquire(str(tmp_path), "foundry") == 0
    assert held_when_stopping == [True]


def test_acquire_refuses_a_card_someone_else_holds(pd, tmp_path, systemctl_calls):
    assert pd.lease_acquire(str(tmp_path), "foundry") == 0
    systemctl_calls.clear()
    assert pd.lease_acquire(str(tmp_path), "coder") == 1
    assert systemctl_calls == []
    assert pd.read_lease(str(tmp_path))["holder"] == "foundry"


def test_acquire_is_idempotent_for_the_same_holder(pd, tmp_path, systemctl_calls):
    assert pd.lease_acquire(str(tmp_path), "foundry") == 0
    assert pd.lease_acquire(str(tmp_path), "foundry") == 0


def test_release_starts_everything_and_clears_only_once_warm(
    pd, tmp_path, monkeypatch, systemctl_calls
):
    pd.lease_acquire(str(tmp_path), "foundry")
    systemctl_calls.clear()
    held_while_loading: list[bool] = []
    monkeypatch.setattr(
        pd,
        "warm_preset",
        lambda url, preset, deadline_sec: (
            bool(held_while_loading.append(_lease(tmp_path, pd).exists())) or True
        ),
    )

    assert pd.lease_release(str(tmp_path), "11434") == 0
    assert systemctl_calls == [("start", pd.LEASED_UNITS)]
    # The lease still stood while the model was loading — a resident asking
    # during those ~38 s gets the honest sentence, not a connection error.
    assert held_while_loading == [True]
    assert not _lease(tmp_path, pd).exists()


def test_release_clears_the_lease_even_when_the_model_never_comes_back(
    pd, tmp_path, monkeypatch, systemctl_calls
):
    """A stuck llama-server must not mute Solaris forever — staying "busy"
    after the holder has left is the worse lie."""
    pd.lease_acquire(str(tmp_path), "foundry")
    monkeypatch.setattr(pd, "warm_preset", lambda url, preset, deadline_sec: False)
    assert pd.lease_release(str(tmp_path), "11434") == 1
    assert not _lease(tmp_path, pd).exists()


def test_lease_file_sits_where_the_chat_pod_mounts_it(pd):
    # templates/solaris/template.yml mounts {{DATA_DIR}}/solarisbay at
    # /var/lib/solaris, which is where solaris_chat.gpu_lease looks for it.
    assert (
        pd.lease_file("/mnt/data/stacks")
        == "/mnt/data/stacks/solarisbay/gpu_lease.json"
    )


# ── #1319: the coding lease ────────────────────────────────────────────────


@pytest.fixture
def swap_box(pd, tmp_path, monkeypatch):
    """A box where the leased weights are already there and the router answers:
    what is left to observe is which units moved and what was written. The
    returned path is the Quadlet a lease must NOT rewrite any more (#1416)."""
    systemd_dir = tmp_path / ".config" / "containers" / "systemd"
    systemd_dir.mkdir(parents=True)
    monkeypatch.setattr(
        pd.os.path, "expanduser", lambda p: p.replace("~", str(tmp_path))
    )
    monkeypatch.setattr(pd, "http_request", lambda *a, **k: (0, b""))
    monkeypatch.setattr(
        pd, "download_model", lambda repo, filename, models_dir, stall: True
    )
    monkeypatch.setattr(pd, "wait_for_ready", lambda url, deadline_sec: True)
    monkeypatch.setattr(pd, "warm_preset", lambda url, preset, deadline_sec: True)
    monkeypatch.setattr(pd, "speculative_active", lambda url: True)
    return systemd_dir / "llama.container"


def _preset(pd, name: str) -> dict[str, str]:
    """One preset section of the rendered file, as `option -> value`."""
    section, body = None, {}
    for line in pd.render_presets("/models").splitlines():
        if line.startswith("["):
            section = line.strip("[]")
        elif line.strip() and section == name:
            key, _, value = line.partition("=")
            body[key] = value
    return body


def test_the_coding_preset_carries_the_levers_that_measured(pd):
    """`--parallel 1` and q8 keys are not tuning: with llama-server's stock four
    slots, or f16 KV, the drafter OOMs before it loads (#1318 cell H1). q4
    values and `-ub 256` are #1415 on image b10920 — 808 MiB freed for 3% and
    5% of prefill — and they pay for the longer draft."""
    preset = _preset(pd, "qwen3.8-27b")
    assert preset["model"] == "/models/Qwen3.8-27B-UD-IQ3_XXS.gguf"
    assert preset["spec-draft-model"] == "/models/mtp-Qwen3.8-27B-Q4_0.gguf"
    assert preset["ctx-size"] == "81920"
    assert preset["cache-type-k"] == "q8_0"
    assert preset["cache-type-v"] == "q4_0"
    assert preset["ubatch-size"] == "256"
    assert preset["parallel"] == "1"
    # +17% generation at 75% drafter acceptance, measured at 64k and paid for
    # by the q4 values at 82k (#1415).
    assert preset["spec-draft-n-max"] == "8"


def test_the_thinking_preset_is_the_moe_the_operator_chose(pd):
    """#1418: 15 620 of 16 380 MiB at 131k q8, 105 tok/s, 83.5% drafter
    acceptance, 12/12 tool calls, and a planted sentence found at 85k."""
    preset = _preset(pd, "qwen3.6-35b-a3b")
    assert preset["model"] == "/models/Qwen3.6-35B-A3B-UD-IQ3_XXS.gguf"
    assert preset["spec-draft-model"] == "/models/mtp-Qwen3.6-35B-A3B-Q4_0.gguf"
    assert preset["ctx-size"] == "131072"
    assert preset["cache-type-k"] == "q8_0"
    assert preset["cache-type-v"] == "q8_0"
    assert preset["parallel"] == "1"
    # Vision was only measured to 98k, and the operator scoped this preset to
    # the 131k text window (#1416).
    assert "mmproj" not in preset


def test_no_preset_switches_thinking_off_at_the_server(pd):
    """#1416: one router serves four models, so a server-wide `--reasoning off`
    would decide for all of them. The switch is per request
    (`chat_template_kwargs.enable_thinking`), box-verified to work in router
    mode on #1415 — which also means a client that sends nothing (aider, goose)
    gets a thinking trace and no tool call. That is the client's setting now."""
    assert "reasoning" not in pd.render_presets("/models")
    assert "--reasoning" not in " ".join(pd.server_args("11434", "/models"))


def test_the_router_argv_carries_no_model_option(pd):
    """A command-line argument outranks a preset option, so a stray `-c` here
    would give all four models the same window."""
    args = pd.server_args("11434", "/models")
    assert args == [
        "--host",
        "127.0.0.1",
        "--port",
        "11434",
        "--models-preset",
        "/models/presets.ini",
        "--models-max",
        "1",
        "--jinja",
    ]


def test_the_presets_file_uses_the_only_syntax_the_box_parses(pd):
    """Box-verified on image b10920 (#1415): `long-option=value`, no leading
    dashes, hyphens rather than underscores. `-m …`, a full command line on one
    line and `ctx_size=` all fail, two of them without naming the line."""
    text = pd.render_presets("/models")
    assert "[gemma-4-e4b]" in text
    for line in text.splitlines():
        if not line.strip() or line.startswith("["):
            continue
        assert not line.startswith("-"), line
        key, sep, _ = line.partition("=")
        assert sep == "=" and "_" not in key and " " not in key, line


def test_the_router_offers_exactly_the_four_presets(pd):
    assert list(pd.preset_profiles()) == [
        "gemma-4-e4b",
        "gemma-4-12b",
        "qwen3.6-35b-a3b",
        "qwen3.8-27b",
    ]


def test_the_household_preset_keeps_its_measured_window(pd):
    """The operator's ruling of 2026-09-13: "e4b ist mit 32k absolut fein"."""
    preset = _preset(pd, "gemma-4-e4b")
    assert preset["ctx-size"] == "32768"
    assert "cache-type-k" not in preset and "parallel" not in preset


def test_the_foundry_preset_takes_the_whole_window_q8_buys(pd):
    """#1415: 131 072 with q8 K+V fits in 10 156 MiB and carried an 85k prompt
    at 686 tok/s with 12/12 tool calls."""
    preset = _preset(pd, "gemma-4-12b")
    assert preset["ctx-size"] == "131072"
    assert preset["cache-type-k"] == "q8_0"
    assert preset["cache-type-v"] == "q8_0"


def test_the_presets_file_lands_where_the_container_reads_it(pd, tmp_path):
    assert pd.presets_file("/mnt/data/stacks").endswith(
        "/mnt/data/stacks/llama/models/presets.ini"
    )
    pd.write_presets(str(tmp_path))
    written = (tmp_path / "llama" / "models" / "presets.ini").read_text()
    assert written == pd.render_presets("/models")


def test_an_extended_acquire_keeps_the_voice_units_running_on_the_cpu(
    pd, tmp_path, swap_box, systemctl_calls
):
    assert pd.lease_acquire(str(tmp_path), "coder", "11434", "erweitert", 3600) == 0
    stopped = [units for verb, units in systemctl_calls if verb == "stop"]
    assert stopped == [pd.LEASE_GPU_UNITS, (pd.EMBED_UNIT,)]
    assert "solaris-whisper.service" not in sum((list(u) for u in stopped), [])
    assert ("restart", pd.LEASE_VOICE_UNITS) in systemctl_calls
    env_file = tmp_path / "solarisbay" / pd.VOICE_DEVICE_FILE
    assert env_file.read_text() == "WHISPER_DEVICE=cpu\nKOKORO_ONNX_PROVIDER=cpu\n"


def test_an_extended_acquire_never_restarts_the_router(
    pd, tmp_path, swap_box, systemctl_calls
):
    """#1416: the router already serves every preset and loads the one the
    holder asks for on its first request. A restart would only cost the
    household its warm model for nothing."""
    assert pd.lease_acquire(str(tmp_path), "coder", "11434", "erweitert", 3600) == 0
    assert ("restart", ("llama.service",)) not in systemctl_calls
    assert ("stop", ("llama.service",)) not in systemctl_calls
    assert not swap_box.exists()


def test_the_lease_carries_the_presets_its_mode_allows(
    pd, tmp_path, swap_box, systemctl_calls
):
    """#1435: in `erweitert` there is nothing left to refuse — every preset the
    router knows is on the menu and the client picks with its `model` field."""
    assert pd.lease_acquire(str(tmp_path), "coder", "11434", "erweitert", 3600) == 0
    lease = pd.read_lease(str(tmp_path))
    assert lease["allowed"] == list(pd.preset_profiles())
    assert lease["ready"] is True
    assert lease["mode"] == "erweitert"
    # Nobody has picked a model yet, so the lease claims none.
    assert lease["alias"] == ""
    assert lease["model"] == ""


def test_there_are_exactly_three_modes_and_this_is_what_they_allow(pd):
    """#1435, operator 2026-09-19. `haushalt` is the absence of a lease and
    allows the household preset; `erweitert` is the open window and allows
    everything; `foundry` keeps its own two because it keeps its own
    environment. `thinking` and `coding` differed from each other only in the
    preset set, which was a distinction without a difference."""
    assert set(pd.LEASE_PROFILES) == {"foundry", "erweitert"}
    assert pd.HOUSEHOLD_MODE == "haushalt" and pd.EXTENDED_MODE == "erweitert"
    presets = set(pd.preset_profiles())
    assert set(pd.allowed_presets("erweitert")) == presets
    assert pd.allowed_presets("foundry") == ("gemma-4-e4b", "gemma-4-12b")
    assert pd.allowed_presets("haushalt") == ("gemma-4-e4b",)
    assert set(pd.allowed_presets("haushalt")) <= presets


def test_foundry_keeps_the_environment_the_chronicle_transcribes_in(pd):
    """The premise behind collapsing all three was that they shared one
    environment. They did not: foundry-chronicle transcribes with
    `solaris-whisper-batch` ON THE GPU during its own session
    (foundry-chronicle#294, #1325), so `foundry` stops neither the batch GPU
    units nor the embeddings server and leaves the voice stack on the card.
    Folding it into `erweitert` would have moved that transcription to the CPU
    without anyone deciding to."""
    foundry = pd.LEASE_PROFILES["foundry"]
    assert foundry["voice"] == "gpu"
    assert foundry["stop_gpu_units"] is False
    assert foundry["stop_embed"] is False
    extended = pd.LEASE_PROFILES["erweitert"]
    assert extended["voice"] == "cpu"
    assert extended["stop_gpu_units"] is True
    assert extended["stop_embed"] is True


def test_the_retired_mode_names_still_reach_the_open_window(pd):
    """pi-web sends `coding` and the reading jobs send `thinking`; they are
    names `erweitert` used to have, not modes of their own. `foundry` is not
    one of them — it is a mode and maps to itself."""
    assert set(pd.LEASE_MODE_ALIASES) == {"thinking", "coding"}
    for old in pd.LEASE_MODE_ALIASES:
        assert pd.canonical_mode(old) == "erweitert"
    assert pd.canonical_mode("erweitert") == "erweitert"
    assert pd.canonical_mode("foundry") == "foundry"
    assert pd.canonical_mode("") == ""


def test_a_named_window_is_answered_by_the_preset_it_always_meant(
    pd, tmp_path, swap_box, systemctl_calls
):
    """The alias is what a caller puts in the `model` field of its own `/v1`
    request (#1333), so every name a caller may still send has to keep meaning
    the model it always meant."""
    for name, mode, alias, label in (
        ("foundry", "foundry", "gemma-4-12b", "Gemma 4 12B"),
        ("thinking", "erweitert", "qwen3.6-35b-a3b", "Qwen 3.6 35B-A3B"),
        ("coding", "erweitert", "qwen3.8-27b", "Qwen 3.8 27B"),
    ):
        assert pd.lease_acquire(str(tmp_path), name, "11434", name, 3600) == 0
        lease = pd.read_lease(str(tmp_path))
        assert lease["mode"] == mode
        assert lease["alias"] == alias
        assert lease["model"] == label
        assert lease["allowed"] == list(pd.allowed_presets(mode))
        pathlib.Path(pd.lease_file(str(tmp_path))).unlink()


def test_a_lease_file_written_under_an_old_name_reads_as_erweitert(pd, tmp_path):
    """A box upgraded mid-window has `coding` on disk. Read as-is it matches no
    profile any more, and the release would leave the voice stack on the CPU
    and the embeddings server down for good."""
    pd.write_lease(str(tmp_path), {"holder": "pi-web", "mode": "coding", "ready": True})
    assert pd.read_lease(str(tmp_path))["mode"] == "erweitert"


def test_the_extended_mode_takes_the_card_off_the_voice_stack(
    pd, tmp_path, swap_box, systemctl_calls
):
    """The 35B-A3B peaks at 15 620 of 16 380 MiB (#1418) — the voice stack has
    to come off the card and the embeddings server has to stop. With one mode
    for every bigger model, that is now the mode's definition (#1435)."""
    assert pd.lease_acquire(str(tmp_path), "reader", "11434", "erweitert", 3600) == 0
    assert ("stop", pd.LEASE_GPU_UNITS) in systemctl_calls
    assert ("stop", (pd.EMBED_UNIT,)) in systemctl_calls
    assert ("restart", pd.LEASE_VOICE_UNITS) in systemctl_calls
    assert ("restart", ("llama.service",)) not in systemctl_calls
    env_file = tmp_path / "solarisbay" / pd.VOICE_DEVICE_FILE
    assert env_file.read_text() == "WHISPER_DEVICE=cpu\nKOKORO_ONNX_PROVIDER=cpu\n"


def test_an_exclusive_lease_allows_no_preset_at_all(pd, tmp_path, systemctl_calls):
    assert pd.lease_acquire(str(tmp_path), "someone") == 0
    assert pd.read_lease(str(tmp_path))["allowed"] == []


def test_every_lease_carries_a_deadline_and_arms_the_expiry(
    pd, tmp_path, swap_box, systemctl_calls, no_box
):
    """#1260's lesson: an end signal alone is not enough. A run that dies
    without releasing must not leave the household on somebody else's model —
    and with two modes (#1435) `erweitert` is the ONLY state in which the
    household is not served, so this is the whole net."""
    before = pd.time.time()
    assert pd.lease_acquire(str(tmp_path), "coder", "11434", "erweitert", 3600) == 0
    lease = pd.read_lease(str(tmp_path))
    assert before + 3600 <= lease["until"] <= pd.time.time() + 3600
    armed = [c for c in no_box if c and c[0] == "systemd-run"]
    assert armed, "no expiry timer was armed"
    assert f"--unit={pd.LEASE_EXPIRY_UNIT}" in armed[0]
    # Not at the deadline but at the grace of two missed renewals (#1361).
    assert "--on-active=2400" in armed[0]
    assert armed[0][-1] == "release"


def test_the_grace_is_two_missed_renewals_and_never_past_the_deadline(pd):
    """#1361: a holder that dies without a DELETE must lose the card in
    minutes, not hours — but a window can still never outlive its own TTL."""
    assert pd.renew_after(900) == 300
    assert pd.expiry_wake(900) == 600
    assert pd.renew_after(14400) == 4800
    assert pd.expiry_wake(14400) == 9600
    # Windows too short for the third to clear the 60 s floor: the deadline
    # itself is the wake, so nothing is armed past it.
    assert pd.expiry_wake(120) == 120
    assert pd.expiry_wake(180) == 120


def test_a_holder_that_keeps_renewing_keeps_its_window(
    pd, tmp_path, swap_box, systemctl_calls, no_box
):
    """The re-arm is the heartbeat: every renewal cancels the pending release
    and arms the next grace, so a live holder is never released underneath.

    Renewed under the OLD name here on purpose (#1435): pi-web sends `coding`
    and the lease on disk says `erweitert`. Compared raw, every heartbeat would
    miss the renewal branch and re-run the whole environment switch instead of
    just moving the deadline."""
    pd.lease_acquire(str(tmp_path), "pi-web", "11434", "coding", 900)
    first = pd.read_lease(str(tmp_path))["last_renewed_at"]
    no_box.clear()
    systemctl_calls.clear()
    pd.lease_acquire(str(tmp_path), "pi-web", "11434", "coding", 900)
    lease = pd.read_lease(str(tmp_path))
    assert lease["last_renewed_at"] >= first
    assert lease["renew_after"] == 300
    assert systemctl_calls == []
    armed = [c for c in no_box if c and c[0] == "systemd-run"]
    assert armed and "--on-active=600" in armed[0]


def test_the_lease_records_the_heartbeat_the_engine_reports(
    pd, tmp_path, swap_box, systemctl_calls
):
    """`GET /api/model-lease` answers these two straight out of the file, so
    the holder can see how long its window survives its own silence."""
    before = pd.time.time()
    pd.lease_acquire(str(tmp_path), "pi-web", "11434", "erweitert", 900)
    lease = pd.read_lease(str(tmp_path))
    assert before <= lease["last_renewed_at"] <= pd.time.time()
    assert lease["renew_after"] == pd.renew_after(900)


def test_the_release_puts_the_gpu_voice_back_and_warms_the_household(
    pd, tmp_path, monkeypatch, swap_box, systemctl_calls
):
    """The router still has the holder's preset resident, so the release asks
    it for the household one: the 9-19 s load is paid here rather than by the
    next resident (#1415/#1416)."""
    warmed: list[str] = []
    monkeypatch.setattr(
        pd,
        "warm_preset",
        lambda url, preset, deadline_sec: bool(warmed.append(preset)) or True,
    )
    pd.lease_acquire(str(tmp_path), "coder", "11434", "erweitert", 3600)
    systemctl_calls.clear()
    assert pd.lease_release(str(tmp_path), "11434") == 0
    assert ("start", pd.LEASE_GPU_UNITS) in systemctl_calls
    assert ("start", (pd.EMBED_UNIT,)) in systemctl_calls
    assert ("restart", pd.LEASE_VOICE_UNITS) in systemctl_calls
    env_file = tmp_path / "solarisbay" / pd.VOICE_DEVICE_FILE
    assert env_file.read_text() == "WHISPER_DEVICE=cuda\nKOKORO_ONNX_PROVIDER=cuda\n"
    assert warmed == ["gemma-4-e4b"]
    assert not swap_box.exists()
    assert not _lease(tmp_path, pd).exists()


def test_release_warms_the_preset_that_was_installed_not_the_default(
    pd, tmp_path, monkeypatch, swap_box, systemctl_calls
):
    """An operator who deployed another household preset name gets that one
    warmed, not this script's default."""
    warmed: list[str] = []
    monkeypatch.setenv("LLAMA_MODEL_ALIAS", "gemma-4-e4b-de")
    pd.save_household_profile(str(tmp_path))
    monkeypatch.delenv("LLAMA_MODEL_ALIAS")
    monkeypatch.setattr(
        pd,
        "warm_preset",
        lambda url, preset, deadline_sec: bool(warmed.append(preset)) or True,
    )
    pd.lease_acquire(str(tmp_path), "coder", "11434", "erweitert", 3600)
    pd.lease_release(str(tmp_path), "11434")
    assert warmed == ["gemma-4-e4b-de"]


def test_missing_weights_stop_nothing(pd, tmp_path, monkeypatch, swap_box):
    """A 12.6 GB download is not something to do with the house muted — the
    weights are fetched before anything is stopped, and a failure is a no-op."""
    monkeypatch.setattr(pd, "download_model", lambda *a: False)
    monkeypatch.setattr(
        pd, "systemctl", lambda verb, units: pytest.fail("stopped a unit anyway")
    )
    assert pd.lease_acquire(str(tmp_path), "coder", "11434", "erweitert", 3600) == 1
    assert not _lease(tmp_path, pd).exists()


def test_an_unknown_model_is_refused_rather_than_run_exclusively(
    pd, tmp_path, systemctl_calls
):
    assert pd.lease_acquire(str(tmp_path), "coder", "11434", "qwen", 3600) == 2
    assert systemctl_calls == []


def test_the_cli_reads_the_holder_the_model_and_the_duration(pd, monkeypatch):
    seen: dict[str, object] = {}
    monkeypatch.setattr(
        pd,
        "lease_acquire",
        lambda d, h, p, m, s: seen.update(holder=h, model=m, seconds=s) or 0,
    )
    assert (
        pd.lease_cli(["acquire", "coder", "--model", "erweitert", "--duration", "4h"])
        == 0
    )
    assert seen == {"holder": "coder", "model": "erweitert", "seconds": 14400}


def test_a_lease_without_a_duration_still_gets_one(pd, monkeypatch):
    seen: dict[str, object] = {}
    monkeypatch.setattr(
        pd, "lease_acquire", lambda d, h, p, m, s: seen.update(seconds=s) or 0
    )
    assert pd.lease_cli(["acquire", "foundry"]) == 0
    assert seen == {"seconds": pd.LEASE_DEFAULT_DURATION_SEC}


def test_a_deploy_during_a_lease_converges_the_unit_but_warms_nothing(
    pd, tmp_path, monkeypatch
):
    """The card stays the holder's — no warm-up asks it for the household
    preset — but the UNIT is mode-independent since #1416 and must converge.
    Skipping it on the v2 -> v3 deploy left the router on the old argv holding
    LLAMA_PORT, the policy proxy crash-looping on a port it could not bind, and
    nothing in the system that would ever have fixed it."""
    installed = []
    monkeypatch.setattr(
        pd, "install_gpu_quadlet_fallback", lambda *a: installed.append(a) or True
    )
    monkeypatch.setattr(pd, "install_embed_unit", lambda *a, **k: True)
    monkeypatch.setattr(
        pd, "warm_preset", lambda *a, **k: pytest.fail("warmed a leased card")
    )
    monkeypatch.setattr(pd, "download_model", lambda *a: True)
    monkeypatch.setattr(pd, "install_lease_script", lambda d: "/x/gpu-lease.py")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LLAMA_GPU_PASSTHROUGH", "true")
    pd.write_lease(str(tmp_path), {"holder": "coder", "mode": "coding"})
    assert pd.main() == 0
    assert installed, "the router unit was left on the pre-deploy argv"


def test_a_deploy_during_an_extended_lease_stops_the_embeddings_server_again(
    pd, tmp_path, monkeypatch, systemctl_calls
):
    """`install_embed_unit` starts the server, and under the MoE that is the
    168 MiB that makes the preset fail to load — so the deploy has to put it
    back the way the lease left it.

    The lease on disk still says `thinking` here: this is the deploy that
    collapses the modes, and the mode it finds is one of the old names (#1435).
    Reading it as unknown would start the embeddings server under the MoE."""
    monkeypatch.setattr(pd, "install_gpu_quadlet_fallback", lambda *a: True)
    monkeypatch.setattr(pd, "install_embed_unit", lambda *a, **k: True)
    monkeypatch.setattr(pd, "warm_preset", lambda *a, **k: True)
    monkeypatch.setattr(pd, "download_model", lambda *a: True)
    monkeypatch.setattr(pd, "install_lease_script", lambda d: "/x/gpu-lease.py")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LLAMA_GPU_PASSTHROUGH", "true")
    pd.write_lease(str(tmp_path), {"holder": "reader", "mode": "thinking"})
    assert pd.main() == 0
    assert ("stop", (pd.EMBED_UNIT,)) in systemctl_calls


def test_both_templates_agree_on_the_voice_env_contract(pd):
    """The lease writes this file; templates/solaris/post-deploy.py's Quadlets
    read it. Two files, one contract — so it is pinned here."""
    solaris_pd = _load("solaris_pd_voice", TEMPLATES / "solaris" / "post-deploy.py")
    assert solaris_pd.VOICE_DEVICE_FILE == pd.VOICE_DEVICE_FILE
    assert solaris_pd.VOICE_DEVICE_ENV == pd.VOICE_DEVICE_ENV
    assert solaris_pd.GPU_LEASE_FILE == pd.LEASE_FILE


# ── #1325: the foundry lease, still its own mode ───────────────────────────


def test_the_foundry_preset_names_the_weights_it_measured_on(pd):
    """#1415: the 12B at 131 072 with q8 K+V fits in 10 156 MiB — 520 MiB more
    than the 32k f16 cell of #1318 — and carried an 85k prompt at 686 tok/s."""
    preset = _preset(pd, "gemma-4-12b")
    assert preset["model"] == "/models/gemma-4-12B-it-Q4_0.gguf"
    assert preset["spec-draft-model"] == "/models/mtp-gemma-4-12B-it-Q8_0.gguf"
    assert "parallel" not in preset and "mmproj" not in preset


def test_the_foundry_window_leaves_the_voice_stack_on_the_card(
    pd, tmp_path, swap_box, systemctl_calls
):
    """#1325 / foundry-chronicle#294: the chronicle transcribes with
    `solaris-whisper-batch` on the GPU DURING its own session, so this is the
    one window that stops nothing and moves nothing to the CPU. #1435 nearly
    folded it into `erweitert`, which would have moved that transcription to
    the CPU without anyone deciding to."""
    assert pd.lease_acquire(str(tmp_path), "foundry", "11434", "foundry", 3600) == 0
    assert systemctl_calls == []
    assert not (tmp_path / "solarisbay" / pd.VOICE_DEVICE_FILE).exists()
    assert not swap_box.exists()


def test_the_extended_window_takes_the_embeddings_server_with_it(
    pd, tmp_path, swap_box, systemctl_calls
):
    """Operator 2026-09-19, measured in #1434: the MoE and `llama-embed` do not
    fit together — 15 620 of 16 380 MiB leaves 760, and the box OOM'd the MTP
    drafter's compute buffer by 168 MiB with the embeddings server's ~430 MiB
    resident. With one window for every bigger model, losing the semantic vault
    search for its duration is part of the decision rather than a surprise."""
    assert pd.lease_acquire(str(tmp_path), "reader", "11434", "erweitert", 3600) == 0
    assert ("stop", pd.LEASE_GPU_UNITS) in systemctl_calls
    assert ("stop", (pd.EMBED_UNIT,)) in systemctl_calls
    assert pd.EMBED_UNIT not in pd.LEASE_GPU_UNITS
    assert pd.EMBED_UNIT in pd.LEASED_UNITS
    assert set(pd.LEASE_GPU_UNITS) == {
        "solaris-whisper-batch.service",
        "solaris-wakeword-trainer.service",
    }


def test_the_release_starts_the_embeddings_server_again(
    pd, tmp_path, monkeypatch, swap_box, systemctl_calls
):
    """A window that took the vault's semantic search away has to give it
    back — otherwise the first such afternoon leaves the household without it
    until someone redeploys."""
    monkeypatch.setattr(pd, "warm_preset", lambda *a, **k: True)
    assert pd.lease_acquire(str(tmp_path), "reader", "11434", "erweitert", 3600) == 0
    systemctl_calls.clear()
    assert pd.lease_release(str(tmp_path), "11434") == 0
    assert ("start", (pd.EMBED_UNIT,)) in systemctl_calls


def test_a_release_of_a_window_taken_under_an_old_name_restores_everything(
    pd, tmp_path, monkeypatch, swap_box, systemctl_calls
):
    """The return path (#1361) has to work for a lease file whose mode is one
    of the retired names — that is exactly what a box upgraded mid-window
    has."""
    warmed: list[str] = []
    monkeypatch.setattr(
        pd,
        "warm_preset",
        lambda url, preset, deadline_sec: bool(warmed.append(preset)) or True,
    )
    pd.lease_acquire(str(tmp_path), "pi-web", "11434", "coding", 3600)
    systemctl_calls.clear()
    assert pd.lease_release(str(tmp_path), "11434") == 0
    assert warmed == ["gemma-4-e4b"]
    assert ("start", pd.LEASE_GPU_UNITS) in systemctl_calls
    assert ("start", (pd.EMBED_UNIT,)) in systemctl_calls
    env_file = tmp_path / "solarisbay" / pd.VOICE_DEVICE_FILE
    assert env_file.read_text() == "WHISPER_DEVICE=cuda\nKOKORO_ONNX_PROVIDER=cuda\n"
    assert not _lease(tmp_path, pd).exists()


def test_a_window_taken_under_an_old_name_expires_back_to_the_household(
    pd, tmp_path, swap_box, systemctl_calls, no_box
):
    assert pd.lease_acquire(str(tmp_path), "pi-web", "11434", "coding", 3600) == 0
    armed = [c for c in no_box if c and c[0] == "systemd-run"]
    assert armed and "--on-active=2400" in armed[0] and armed[0][-1] == "release"


def test_the_cli_knows_every_mode_and_every_old_name(pd, monkeypatch):
    seen: dict[str, object] = {}
    monkeypatch.setattr(
        pd,
        "lease_acquire",
        lambda d, h, p, m, s: seen.update(holder=h, model=m, seconds=s) or 0,
    )
    for mode in (*pd.LEASE_PROFILES, *pd.LEASE_MODE_ALIASES):
        assert pd.lease_cli(["acquire", "someone", "--model", mode]) == 0
        assert seen["model"] == mode


# ── #1333: the HTTP lease — --alias, the renewal, and the host broker ──────


def test_every_preset_names_itself_in_the_v1_responses(pd):
    """foundry reads the `model` field of the answer to record which model
    wrote a chronicle entry; without `alias` that field is a GGUF path. In
    router mode the section name is also what a client has to ask for, so the
    two must be the same string."""
    for name, profile in pd.preset_profiles().items():
        assert _preset(pd, name)["alias"] == name == profile["alias"]


def test_both_argv_sources_load_the_same_presets_file(pd):
    """`server_args` renders the Quadlet on a GPU box, template.yml the kube
    unit everywhere else — a router that only one of them starts is a model
    list a neighbour cannot rely on."""
    tmpl = (TEMPLATES / "llama" / "template.yml").read_text(encoding="utf-8")
    assert '- "--models-preset"' in tmpl
    assert '- "/models/presets.ini"' in tmpl
    assert '- "--models-max"' in tmpl
    assert '- "-m"' not in tmpl and '- "--alias"' not in tmpl
    assert "--models-preset /models/presets.ini" in " ".join(
        pd.server_args("11434", "/models")
    )
    variables = json.loads(
        (TEMPLATES / "llama" / "variables.json").read_text(encoding="utf-8")
    )
    assert variables["LLAMA_MODEL_ALIAS"]["default"] == "gemma-4-e4b"
    assert pd.env_profile()["alias"] == variables["LLAMA_MODEL_ALIAS"]["default"]


def test_the_lease_records_the_alias_the_holder_will_be_answered_by(
    pd, tmp_path, swap_box, systemctl_calls
):
    pd.lease_acquire(str(tmp_path), "foundry", "11434", "foundry", 3600)
    assert pd.read_lease(str(tmp_path))["alias"] == "gemma-4-12b"


def test_a_renewal_moves_the_deadline_without_swapping_again(
    pd, tmp_path, swap_box, systemctl_calls, no_box
):
    """The holder renews every few minutes. Reloading llama-server each time
    would cost the household a cold load per renewal — the deadline moves, the
    server does not."""
    pd.lease_acquire(str(tmp_path), "foundry", "11434", "foundry", 3600)
    first_until = pd.read_lease(str(tmp_path))["until"]
    systemctl_calls.clear()
    no_box.clear()
    assert pd.lease_acquire(str(tmp_path), "foundry", "11434", "foundry", 7200) == 0
    assert systemctl_calls == []
    assert pd.read_lease(str(tmp_path))["until"] > first_until
    armed = [c for c in no_box if c and c[0] == "systemd-run"]
    assert armed and "--on-active=4800" in armed[0]


def _request(pd, tmp_path, **fields) -> None:
    path = pathlib.Path(pd.request_file(str(tmp_path)))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(fields))


def test_the_broker_acquires_what_the_engine_asked_for(
    pd, tmp_path, swap_box, systemctl_calls
):
    _request(
        pd,
        tmp_path,
        op="acquire",
        model="foundry",
        ttl_s=900,
        holder="foundry",
        requested_at=1.5,
    )
    assert pd.broker_run(str(tmp_path), "11434") == 0
    lease = pd.read_lease(str(tmp_path))
    assert lease["mode"] == "foundry" and lease["holder"] == "foundry"
    status = json.loads(pathlib.Path(pd.status_file(str(tmp_path))).read_text())
    # The requested_at goes back unchanged — that is how the HTTP side knows
    # this request has been dealt with and is not still "preparing".
    assert status["requested_at"] == 1.5
    assert status["state"] == "ready"
    assert status["alias"] == "gemma-4-12b"
    assert status["expires_at"] == lease["until"]


def test_the_broker_files_the_window_under_the_service_that_asked(
    pd, tmp_path, swap_box, systemctl_calls
):
    """#1347: the Engine passes the caller's own name through, so the lease on
    the box says who holds it and a stranger's `release` is refused here too."""
    _request(
        pd,
        tmp_path,
        op="acquire",
        model="foundry",
        ttl_s=900,
        holder="foundry-chronicle",
        requested_at=1.75,
    )
    assert pd.broker_run(str(tmp_path), "11434") == 0
    assert pd.read_lease(str(tmp_path))["holder"] == "foundry-chronicle"
    status = json.loads(pathlib.Path(pd.status_file(str(tmp_path))).read_text())
    assert status["holder"] == "foundry-chronicle"
    # An acquire without a holder stays what it has always been: the profile.
    pathlib.Path(pd.lease_file(str(tmp_path))).unlink()
    _request(pd, tmp_path, op="acquire", model="foundry", ttl_s=900, requested_at=1.85)
    assert pd.broker_run(str(tmp_path), "11434") == 0
    assert pd.read_lease(str(tmp_path))["holder"] == "foundry"


def test_the_broker_releases_and_says_which_model_is_back(
    pd, tmp_path, swap_box, systemctl_calls
):
    pd.lease_acquire(str(tmp_path), "foundry", "11434", "foundry", 3600)
    _request(pd, tmp_path, op="release", model="", requested_at=2.5)
    assert pd.broker_run(str(tmp_path), "11434") == 0
    assert not _lease(tmp_path, pd).exists()
    status = json.loads(pathlib.Path(pd.status_file(str(tmp_path))).read_text())
    assert status["state"] == "released"
    assert status["alias"] == "gemma-4-e4b"
    assert status["requested_at"] == 2.5


def test_a_failed_acquire_is_reported_rather_than_left_pending(
    pd, tmp_path, monkeypatch, swap_box
):
    """A holder polling GET must find out; a silent failure would leave it
    waiting for a window that is never coming."""
    monkeypatch.setattr(pd, "download_model", lambda *a: False)
    _request(pd, tmp_path, op="acquire", model="foundry", ttl_s=900, requested_at=3.5)
    assert pd.broker_run(str(tmp_path), "11434") == 0
    status = json.loads(pathlib.Path(pd.status_file(str(tmp_path))).read_text())
    assert status["state"] == "error"
    assert status["expires_at"] is None


def test_an_unknown_request_never_reaches_the_units(pd, tmp_path, systemctl_calls):
    _request(pd, tmp_path, op="acquire", model="llama5", requested_at=4.5)
    assert pd.broker_run(str(tmp_path), "11434") == 0
    assert systemctl_calls == []
    assert not _lease(tmp_path, pd).exists()


def test_no_request_at_all_is_a_no_op(pd, tmp_path, systemctl_calls):
    assert pd.broker_run(str(tmp_path), "11434") == 0
    assert systemctl_calls == []
    assert not pathlib.Path(pd.status_file(str(tmp_path))).exists()


def test_the_broker_units_watch_the_file_the_engine_writes(pd, tmp_path):
    path_unit, service_unit = render = pd.render_broker_units(
        str(tmp_path), "11434", "/x/gpu-lease.py"
    )
    assert len(render) == 2
    assert f"PathChanged={pd.request_file(str(tmp_path))}" in path_unit
    assert f"Unit={pd.BROKER_UNIT}.service" in path_unit
    assert "/x/gpu-lease.py broker" in service_unit
    assert f"Environment=DATA_DIR={tmp_path}" in service_unit


def test_installing_the_broker_enables_the_watcher(pd, tmp_path, monkeypatch, no_box):
    calls: list[tuple[str, tuple[str, ...]]] = []
    monkeypatch.setattr(
        pd, "systemctl", lambda verb, units: bool(calls.append((verb, units))) or True
    )
    monkeypatch.setattr(
        pd.os.path, "expanduser", lambda p: p.replace("~", str(tmp_path))
    )
    pd.install_broker_units(str(tmp_path), "11434", "/x/gpu-lease.py")
    unit_dir = tmp_path / ".config" / "systemd" / "user"
    assert (unit_dir / f"{pd.BROKER_UNIT}.path").exists()
    assert (unit_dir / f"{pd.BROKER_UNIT}.service").exists()
    assert calls == [("enable", ("--now", f"{pd.BROKER_UNIT}.path"))]


# ── #1416: what the install declares, and the v2 → v3 hop ──────────────────


def test_the_install_declares_every_preset_it_offers(pd, tmp_path, monkeypatch):
    """The router lists all four presets from its first start, so a file that
    is not fetched is a 500 on the resident's turn rather than a model that is
    merely unavailable. The thinking weights are already on the box (#1418);
    the code still has to name them, or a fresh install has no MoE."""
    asked: list[tuple[str, str]] = []
    monkeypatch.setattr(
        pd,
        "download_model",
        lambda repo, filename, models_dir, stall: (
            bool(asked.append((repo, filename))) or True
        ),
    )
    monkeypatch.setenv("LLAMA_MMPROJ_FILE", "mmproj-gemma-4-E4B-it-Q8_0.gguf")
    assert pd.ensure_preset_weights(str(tmp_path), list(pd.preset_profiles()))
    assert ("unsloth/Qwen3.6-35B-A3B-GGUF", "Qwen3.6-35B-A3B-UD-IQ3_XXS.gguf") in asked
    assert ("ggml-org/Qwen3.6-35B-A3B-GGUF", "mtp-Qwen3.6-35B-A3B-Q4_0.gguf") in asked
    # The household projector comes from the model repo, which the profile
    # leaves blank for its drafter and its mmproj.
    assert (
        "ggml-org/gemma-4-E4B-it-GGUF",
        "mmproj-gemma-4-E4B-it-Q8_0.gguf",
    ) in asked
    assert len(asked) == 9


def test_a_missing_file_is_reported_rather_than_silently_skipped(
    pd, tmp_path, monkeypatch
):
    monkeypatch.setattr(pd, "download_model", lambda *a: False)
    assert not pd.ensure_preset_weights(str(tmp_path), ["qwen3.6-35b-a3b"])


def test_the_v2_to_v3_migration_splits_the_recorded_cache_type(pd, tmp_path):
    """A v2 box recorded the household profile with one `cache_type` and a
    `reasoning` key. v3 has separate key and value types and no reasoning."""
    migration = _load(
        "llama_v2_to_v3", TEMPLATES / "llama" / "migrations" / "v2-to-v3.py"
    )
    path = tmp_path / "solarisbay" / "llama-profile.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"alias": "gemma-4-e4b", "cache_type": "q8_0", "reasoning": "off"})
    )
    migration.migrate(str(path))
    record = json.loads(path.read_text())
    assert record == {
        "alias": "gemma-4-e4b",
        "cache_type_k": "q8_0",
        "cache_type_v": "q8_0",
        "ubatch": "",
    }
    # And what post-deploy reads back out of it is the v3 shape.
    assert set(pd.env_profile()) >= set(record)


def test_the_v2_to_v3_migration_is_a_no_op_without_a_recorded_profile(tmp_path):
    migration = _load(
        "llama_v2_to_v3_empty", TEMPLATES / "llama" / "migrations" / "v2-to-v3.py"
    )
    assert "nothing to migrate" in migration.migrate(str(tmp_path / "absent.json"))

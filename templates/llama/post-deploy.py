#!/usr/bin/env python3
"""
post-deploy hook for the `llama` template.

Seven responsibilities:

  1. **Download the GGUFs and write the presets file.** llama-server serves a
     file, not a registry — nothing pulls on first start. The weights, the MTP
     drafters and the multimodal projector of all four presets are fetched
     from Hugging Face into ${DATA_DIR}/llama/models before the server is
     expected to come up, and `presets.ini` beside them is what the router
     reads (#1416): one process, one port, four models, the client picks with
     the `model` field of its request.

  2. **Get the container onto the GPU.** `podman kube play` drops
     `resources.limits.nvidia.com/gpu`, and on rootless FCoS the CDI device
     alone is not enough — without `SecurityLabelDisable=true` llama-server
     logs one passing "no usable GPU found" warning and answers from the CPU.
     Both lines are load-bearing; #1026 hit the same wall.

  3. **Run the embeddings server** (#1332). A second, small llama-server
     instance serves `nomic-embed-text` on `--embeddings`, which is the last
     job Ollama still had. Its own `llama-embed.container` Quadlet, loopback
     bind, ~300 MB of VRAM.

  4. **Serve the mode policy on LLAMA_PORT** (#1416). The router enforces
     nothing — asked for a preset, it loads it, evicting whatever was resident.
     So the router moved to LLAMA_ROUTER_PORT on loopback and this script's
     `proxy` verb took its place on LLAMA_PORT: it reads the lease's `allowed`
     set per request, refuses a preset outside it with 409, marks every preset
     in `/v1/models` as usable in the standing mode or not, and forwards
     everything else to the router, chunk by chunk. Plus an HTTP health check
     against `/health`, which passes through it.

  5. **Install the GPU lease** (#1320, #1319, #1325). A copy of this script
     lands at `${DATA_DIR}/solarisbay/gpu-lease.py`; run with `acquire
     <holder>` it hands the whole card to another job, with `release` it gives
     it back. Self-copy, like ollama-warm (#1236), so the unit list cannot
     drift from a second copy of itself. `--model erweitert` takes the softer
     path: llama-server keeps serving all four presets, and the mode only sets
     the environment and the presets a client may ask for (#1416/#1435).

  6. **Install the lease broker** (#1333). A neighbour *container* cannot run
     any of that, so it asks the Engine over HTTP instead; the Engine writes
     `${DATA_DIR}/solarisbay/gpu_lease_request.json` and
     `solaris-gpu-lease-broker.path` runs this script's `broker` verb, which
     performs the same `acquire`/`release` and reports back in
     `gpu_lease_status.json`. The request's `holder` (#1347) is passed straight
     through as the `acquire <holder>` above, so a window on the box is filed
     under the service that asked for it and not under the profile name.

Idempotent: a second run finds the files on disk and skips the download; the
Quadlet is re-activated only when it isn't the live unit source; the
health-check API does upsert-by-id; the lease script is rewritten in place.

See lib/registry.ts:getTemplatePostDeployScript for the script protocol and
docs/TEMPLATE_AUTHORING.md § Health checks for the check-registration
contract.
"""

from __future__ import annotations

import datetime
import http.client
import http.server
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

PROGRESS_LOG_INTERVAL_SEC = 15
DOWNLOAD_CHUNK = 1024 * 1024

# The router's preset file (#1416), written beside the weights so the one
# volume the container already mounts carries it too.
PRESETS_FILE = "presets.ini"


def env(key: str, default: str = "") -> str:
    val = os.environ.get(key, default)
    return val if val else default


def jlog(level: str, tag: str, message: str, **args: object) -> None:
    """Emit a TEMPLATE_LOGGING.md-shaped line on stdout."""
    sys.stdout.write(
        json.dumps(
            {
                "ts": datetime.datetime.now().astimezone().isoformat(),
                "level": level,
                "tag": tag,
                "message": message,
                "args": args,
            }
        )
        + "\n"
    )
    sys.stdout.flush()


def http_request(
    url: str,
    payload: dict[str, object] | None = None,
    method: str = "GET",
    timeout: float = 10.0,
    extra_headers: dict[str, str] | None = None,
) -> tuple[int, bytes]:
    headers = {"Content-Type": "application/json"}
    if extra_headers:
        headers.update(extra_headers)
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        try:
            body = e.read()
        except Exception:  # pylint: disable=broad-except
            body = b""
        return e.code, body
    except (urllib.error.URLError, TimeoutError, OSError):
        return 0, b""


def model_url(repo: str, filename: str) -> str:
    return f"https://huggingface.co/{repo}/resolve/main/{filename}?download=true"


def download_model(repo: str, filename: str, models_dir: str, stall_sec: int) -> bool:
    """Fetch one GGUF into `models_dir`, unless it is already there.

    Writes to `<name>.part` and renames on completion, so an interrupted
    download can never be mistaken for a usable model file — llama-server
    would otherwise start against a truncated GGUF and crash-loop with a
    parse error that says nothing about the real cause.
    """
    dest = os.path.join(models_dir, filename)
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        jlog(
            "info",
            "llama:models",
            "model file already present",
            file=filename,
            size_mb=os.path.getsize(dest) // (1024 * 1024),
        )
        return True
    part = f"{dest}.part"
    url = model_url(repo, filename)
    jlog("info", "llama:models", "downloading model file", file=filename, url=url)
    started = time.time()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "solarisbay"})
        with (
            urllib.request.urlopen(req, timeout=stall_sec) as resp,
            open(part, "wb") as out,
        ):
            total = int(resp.headers.get("Content-Length") or 0)
            done = 0
            last_log = 0.0
            while True:
                chunk = resp.read(DOWNLOAD_CHUNK)
                if not chunk:
                    break
                out.write(chunk)
                done += len(chunk)
                now = time.time()
                if now - last_log >= PROGRESS_LOG_INTERVAL_SEC:
                    pct = int(done * 100 / total) if total else 0
                    filled = pct * 20 // 100
                    bar = "#" * filled + "-" * (20 - filled)
                    jlog(
                        "info",
                        "llama:models",
                        f"{filename} [{bar}] {pct}% "
                        f"({done // (1024 * 1024)}/{total // (1024 * 1024)} MB)",
                        file=filename,
                        percent=pct,
                        completed_mb=done // (1024 * 1024),
                        total_mb=total // (1024 * 1024),
                    )
                    last_log = now
        if total and done < total:
            raise OSError(f"short read: {done} of {total} bytes")
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
        jlog(
            "error",
            "llama:models",
            "download failed",
            file=filename,
            url=url,
            error=str(e),
        )
        try:
            os.unlink(part)
        except OSError:
            pass
        return False
    os.replace(part, dest)
    jlog(
        "info",
        "llama:models",
        "model file ready",
        file=filename,
        size_mb=os.path.getsize(dest) // (1024 * 1024),
        elapsed_sec=int(time.time() - started),
    )
    return True


def ensure_preset_weights(data_dir: str, presets: tuple[str, ...] | list[str]) -> bool:
    """Fetch every file the named presets serve from, unless it is there.

    The thinking preset's 14 GB are already on the box (#1418), but a preset
    the router lists and cannot load is a 500 on the resident's turn, so the
    files are declared here like every other one.
    """
    profiles = preset_profiles()
    models_dir = os.path.join(data_dir, "llama", "models")
    stall_sec = int(env("LLAMA_DOWNLOAD_STALL_SECONDS", "600"))
    complete = True
    for name in presets:
        profile = profiles[name]
        for repo_key, file_key in (
            ("model_repo", "model_file"),
            ("draft_repo", "draft_file"),
            ("model_repo", "mmproj_file"),
        ):
            filename = profile[file_key]
            repo = profile[repo_key] or profile["model_repo"]
            if not filename:
                continue
            if not download_model(repo, filename, models_dir, stall_sec):
                jlog(
                    "warn",
                    "llama:models",
                    "model file missing — the preset that needs it cannot load. Download it manually into %s from https://huggingface.co/%s"
                    % (models_dir, repo),
                    preset=name,
                    file=filename,
                )
                complete = False
    return complete


def wait_for_ready(llama_url: str, deadline_sec: int) -> bool:
    """Poll /health until llama-server answers 200 (model + drafter loaded)."""
    started = time.time()
    last_beat = 0.0
    while time.time() - started < deadline_sec:
        status, _ = http_request(f"{llama_url}/health", timeout=5)
        if status == 200:
            return True
        elapsed = time.time() - started
        if elapsed - last_beat >= 10:
            jlog(
                "info",
                "llama:wait",
                "still waiting for llama-server",
                elapsed_sec=int(elapsed),
            )
            last_beat = elapsed
        time.sleep(3)
    return False


def speculative_active(llama_url: str, preset: str) -> bool:
    """True when /slots reports the drafter is actually in play.

    The server starts happily without speculative decoding when the draft
    model is missing or the flags are wrong, and then just runs at half
    speed — a silent regression with no error anywhere (#1317/#1318).

    `preset` names which child to ask: a router answers a bare `/slots` with
    400 "model name is missing from the request", which read as "no drafter"
    and warned on every deploy while all four presets had one (box, 13.9.).
    """
    status, body = http_request(
        f"{llama_url}/slots?model={urllib.parse.quote(preset)}", timeout=5
    )
    if status != 200:
        return False
    try:
        slots = json.loads(body.decode("utf-8") or "[]")
    except json.JSONDecodeError:
        return False
    return any(bool(s.get("speculative")) for s in slots if isinstance(s, dict))


def gpu_container_is_live_source() -> bool:
    """True iff the active `llama.service` is generated from the GPU
    `.container` Quadlet and not from the CPU `.kube`. A redeploy re-creates
    `llama.kube` and flips the active service back, so a byte-identical
    `.container` file on disk is not evidence the GPU unit is live — this is."""
    try:
        out = subprocess.run(
            ["systemctl", "--user", "show", "-p", "SourcePath", "llama.service"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.SubprocessError, OSError):
        return False
    return out.stdout.strip().endswith("llama.container")


def env_profile() -> dict[str, str]:
    """The household server profile, as the template variables describe it."""
    return {
        "model_repo": env("LLAMA_MODEL_REPO", "ggml-org/gemma-4-E4B-it-GGUF"),
        "model_file": env("LLAMA_MODEL_FILE", "gemma-4-E4B-it-Q4_0.gguf"),
        "draft_repo": "",
        "draft_file": env("LLAMA_DRAFT_FILE", "mtp-gemma-4-E4B-it-Q8_0.gguf"),
        "mmproj_file": env("LLAMA_MMPROJ_FILE", ""),
        "context_length": env("LLAMA_CONTEXT_LENGTH", "32768"),
        "draft_n_max": env("LLAMA_DRAFT_N_MAX", "4"),
        "cache_type_k": "",
        "cache_type_v": "",
        "ubatch": "",
        "parallel": "",
        "alias": env("LLAMA_MODEL_ALIAS", "gemma-4-e4b"),
        "label": "Gemma 4 E4B",
    }


def server_args(port: str, models_dir_in_container: str) -> list[str]:
    """The llama-server argv, shared by the Quadlet render and template.yml.

    `port` is LLAMA_ROUTER_PORT, not LLAMA_PORT.

    Router mode (#1416): one process, one port, four presets, and the client
    picks with the `model` field of its request. Everything a model needs —
    weights, window, KV types, drafter, projector — lives in the presets file
    below, so nothing model-shaped may appear here: a command-line argument
    outranks a preset option (llama.cpp `docs/preset.md`) and would silently
    apply one model's window to all four. A child instance inherits the rest
    of this argv, which is how `--jinja` reaches every preset.
    """
    return [
        # Loopback, and the router's own port. The wide bind #1344 needed for
        # `host.containers.internal` moved to the policy proxy, which is what
        # holds LLAMA_PORT now: the router will load any preset it is asked
        # for, so nothing but the proxy may be able to ask it (#1416).
        "--host",
        "127.0.0.1",
        "--port",
        port,
        "--models-preset",
        f"{models_dir_in_container}/{PRESETS_FILE}",
        # One model resident at a time: the card holds exactly one of these
        # (#1415/#1418). The router evicts the idle LRU child and loads the
        # asked-for preset in 9-19 s rather than OOMing on both.
        "--models-max",
        "1",
        "--jinja",
    ]


def preset_profiles() -> dict[str, dict[str, str]]:
    """The four presets the router offers, keyed by the name a client asks for.

    The key is the section name in the presets file, which is what `GET
    /v1/models` lists and what the `model` field of a request has to carry.
    """
    return {
        profile["alias"]: profile
        for profile in (
            env_profile(),
            FOUNDRY_PROFILE,
            THINKING_PROFILE,
            CODING_PROFILE,
        )
    }


def preset_lines(profile: dict[str, str], models_dir_in_container: str) -> list[str]:
    """One preset's options, in the only syntax the router parses.

    Box-verified on image b10920 (#1415): `long-option=value`, no leading
    dashes, hyphens rather than underscores. Short flags, a whole command line
    on one line and `ctx_size=` all fail — two of them with a message that
    does not name the offending line.
    """
    lines = [
        f"model={models_dir_in_container}/{profile['model_file']}",
        f"ctx-size={profile['context_length']}",
        "n-gpu-layers=99",
        f"alias={profile['alias']}",
    ]
    if profile["cache_type_k"]:
        lines.append(f"cache-type-k={profile['cache_type_k']}")
    if profile["cache_type_v"]:
        lines.append(f"cache-type-v={profile['cache_type_v']}")
    if profile["ubatch"]:
        lines.append(f"ubatch-size={profile['ubatch']}")
    if profile["parallel"]:
        lines.append(f"parallel={profile['parallel']}")
    if profile["draft_file"]:
        lines += [
            "spec-type=draft-mtp",
            f"spec-draft-model={models_dir_in_container}/{profile['draft_file']}",
            "spec-draft-ngl=99",
            f"spec-draft-n-max={profile['draft_n_max']}",
        ]
    if profile["mmproj_file"]:
        lines.append(f"mmproj={models_dir_in_container}/{profile['mmproj_file']}")
    return lines


def render_presets(models_dir_in_container: str) -> str:
    """The whole presets file the router loads at start."""
    blocks = []
    for name, profile in preset_profiles().items():
        body = "\n".join(preset_lines(profile, models_dir_in_container))
        blocks.append(f"[{name}]\n{body}\n")
    return "\n".join(blocks)


def write_presets(data_dir: str) -> bool:
    """Put the presets file next to the weights, where the container sees it
    as `/models/presets.ini`."""
    path = presets_file(data_dir)
    text = render_presets("/models")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(path, 0o644)
    except OSError as e:
        jlog(
            "error",
            "llama:presets",
            "could not write the presets file; llama-server has no model to serve",
            path=path,
            error=str(e),
        )
        return False
    jlog(
        "info",
        "llama:presets",
        "presets written",
        path=path,
        presets=list(preset_profiles()),
    )
    return True


def render_gpu_container_unit(port: str, data_dir: str) -> str:
    """Render the `.container` Quadlet text for the GPU fixup. Pure, so the
    needs-rewrite comparison and the write share one source of truth."""
    exec_args = " ".join(server_args(port, "/models"))
    return (
        "[Unit]\n"
        "Description=llama.cpp llama-server (household model, GPU passthrough)\n"
        "Wants=network-online.target\n"
        "After=network-online.target\n"
        "\n"
        "[Container]\n"
        "Image=ghcr.io/ggml-org/llama.cpp:server-cuda\n"
        "ContainerName=llama\n"
        "Network=host\n"
        f"Exec={exec_args}\n"
        "# CDI device — podman kube play silently drops this when it is\n"
        "# expressed as resources.limits.nvidia.com/gpu, which is why the\n"
        "# .yml-based deploy falls through to CPU. See #1026.\n"
        "AddDevice=nvidia.com/gpu=all\n"
        "# Without the SELinux relaxation the container sees the device but\n"
        "# NVML cannot init: llama-server logs one passing 'no usable GPU\n"
        "# found', loads the model into RAM and answers from the CPU at a\n"
        "# fraction of the speed, with nothing in any log that reads as an\n"
        "# error. Box-measured 2026-09-04 (#1318).\n"
        "SecurityLabelDisable=true\n"
        f"Volume={data_dir}/llama/models:/models:Z\n"
        "AutoUpdate=registry\n"
        "\n"
        "[Service]\n"
        "Restart=on-failure\n"
        "RestartSec=5\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def install_gpu_quadlet_fallback(port: str, data_dir: str) -> bool:
    """Replace the deployed rootless `.kube` llama unit with a `.container`
    Quadlet carrying `AddDevice=nvidia.com/gpu=all` + `SecurityLabelDisable=
    true` — the only combination on rootless podman 5.x that actually gets
    CDI passthrough plus the SELinux relaxation NVML init needs.

    Idempotent on both the file and the active unit: a matching file whose
    `.container` is the live source with no `.kube` lingering is the only
    no-op."""
    if not os.path.exists("/etc/cdi/nvidia.yaml"):
        jlog(
            "info",
            "llama:gpu-fallback",
            "/etc/cdi/nvidia.yaml missing; CDI not registered on this host. Leaving the CPU-only kube unit in place.",
        )
        return False

    systemd_dir = os.path.expanduser("~/.config/containers/systemd")
    kube_path = os.path.join(systemd_dir, "llama.kube")
    container_path = os.path.join(systemd_dir, "llama.container")
    container_unit = render_gpu_container_unit(port, data_dir)

    content_matches = False
    if os.path.exists(container_path):
        try:
            with open(container_path) as f:
                existing = f.read()
        except OSError:
            existing = ""
        content_matches = existing == container_unit

    if (
        content_matches
        and gpu_container_is_live_source()
        and not os.path.exists(kube_path)
    ):
        jlog(
            "info",
            "llama:gpu-fallback",
            "llama.container already live (GPU source, no llama.kube); no-op",
            path=container_path,
        )
        return True

    subprocess.run(
        ["systemctl", "--user", "stop", "llama.service"],
        check=False,
        capture_output=True,
    )
    if os.path.exists(kube_path):
        try:
            os.unlink(kube_path)
        except OSError as e:
            jlog(
                "warn",
                "llama:gpu-fallback",
                "could not remove llama.kube — Quadlet may complain",
                path=kube_path,
                error=str(e),
            )
    if not content_matches:
        try:
            with open(container_path, "w") as f:
                f.write(container_unit)
            os.chmod(container_path, 0o644)
        except OSError as e:
            jlog(
                "error",
                "llama:gpu-fallback",
                "could not write llama.container",
                path=container_path,
                error=str(e),
            )
            return False
    subprocess.run(
        ["systemctl", "--user", "daemon-reload"], check=False, capture_output=True
    )
    started = subprocess.run(
        ["systemctl", "--user", "start", "llama.service"],
        capture_output=True,
        text=True,
    )
    if started.returncode != 0:
        jlog(
            "error",
            "llama:gpu-fallback",
            "systemctl start failed",
            stderr=started.stderr[:400],
        )
        return False
    jlog(
        "info",
        "llama:gpu-fallback",
        "swapped rootless llama.kube -> llama.container for CDI passthrough",
        path=container_path,
    )
    return True


# --- The embeddings server (#1332) ----------------------------------------
#
# The vault's semantic search and the OKF vector store need `nomic-embed-text`.
# Ollama used to serve it, and that was the only reason the service was still
# installed at all. A second, small llama-server does the same job on the same
# card: `--embeddings` turns on OpenAI `/v1/embeddings`, and 274 MB of f16
# weights cost about 300 MB of VRAM.
#
# Two settings are not tuning and must not be "simplified":
#   * `--pooling mean` — nomic-embed-text is a mean-pooled model. Any other
#     pooling produces valid-looking vectors that do not match the ~46k rows
#     already in `okf_vectors`, and search would quietly get worse rather than
#     fail.
#   * `--ubatch-size` == the context length — an embedding model runs
#     non-causal attention, and llama.cpp rejects a request longer than one
#     micro-batch outright.
EMBED_UNIT = "llama-embed.service"
EMBED_CONTAINER = "llama-embed.container"


def embed_profile() -> dict[str, str]:
    """The embeddings server profile, as the template variables describe it."""
    return {
        # Read raw, not through env(): that helper folds an empty value back
        # to the default, and an empty LLAMA_EMBED_PORT is how an operator
        # turns the embeddings server off.
        "port": os.environ.get("LLAMA_EMBED_PORT", "11436").strip(),
        "model_repo": env("LLAMA_EMBED_REPO", "nomic-ai/nomic-embed-text-v1.5-GGUF"),
        "model_file": env("LLAMA_EMBED_FILE", "nomic-embed-text-v1.5.f16.gguf"),
        "alias": env("LLAMA_EMBED_ALIAS", "nomic-embed-text"),
        "context_length": env("LLAMA_EMBED_CONTEXT_LENGTH", "2048"),
    }


def embed_server_args(
    models_dir_in_container: str, profile: dict[str, str] | None = None
) -> list[str]:
    """The llama-server argv for the embeddings instance.

    Loopback only: its one consumer is the Solaris Engine, which runs in this
    host's network namespace. Unlike the chat server (#1344) no pod reaches it,
    so there is nothing to open the LAN-facing bind for.
    """
    profile = profile or embed_profile()
    return [
        "--host",
        "127.0.0.1",
        "--port",
        profile["port"],
        "-m",
        f"{models_dir_in_container}/{profile['model_file']}",
        "-ngl",
        "99",
        "-c",
        profile["context_length"],
        "--batch-size",
        profile["context_length"],
        "--ubatch-size",
        profile["context_length"],
        "--pooling",
        "mean",
        "--embeddings",
        "--alias",
        profile["alias"],
    ]


def render_embed_container_unit(
    data_dir: str, gpu: bool, profile: dict[str, str] | None = None
) -> str:
    """Render the `llama-embed.container` Quadlet text. Pure, so the
    needs-rewrite comparison and the write share one source of truth."""
    exec_args = " ".join(embed_server_args("/models", profile))
    gpu_lines = (
        "AddDevice=nvidia.com/gpu=all\n"
        "# Same pair as llama.container: the device alone leaves NVML unable to\n"
        "# init under SELinux and llama-server embeds from the CPU instead,\n"
        "# with nothing in any log that reads as an error (#1318).\n"
        "SecurityLabelDisable=true\n"
        if gpu
        else ""
    )
    return (
        "[Unit]\n"
        "Description=llama.cpp llama-server (embeddings, nomic-embed-text)\n"
        "Wants=network-online.target\n"
        "After=network-online.target\n"
        "\n"
        "[Container]\n"
        "Image=ghcr.io/ggml-org/llama.cpp:server-cuda\n"
        "ContainerName=llama-embed\n"
        "Network=host\n"
        f"Exec={exec_args}\n"
        f"{gpu_lines}"
        f"Volume={data_dir}/llama/models:/models:Z\n"
        "AutoUpdate=registry\n"
        "\n"
        "[Service]\n"
        "Restart=on-failure\n"
        "RestartSec=5\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def install_embed_unit(data_dir: str, gpu: bool) -> bool:
    """Install/refresh `llama-embed.container` and make sure it is running.

    Idempotent: an unchanged unit file is only started, not rewritten. The
    embeddings server is a Quadlet of its own rather than a second container in
    the pod, because the GPU fixup replaces the pod's `.kube` unit outright and
    a pod sibling would be dropped with it.
    """
    profile = embed_profile()
    if not profile["port"]:
        jlog(
            "info",
            "llama:embed",
            "LLAMA_EMBED_PORT is empty; no embeddings server. The vault's semantic search falls back to keyword search.",
        )
        return False
    systemd_dir = os.path.expanduser("~/.config/containers/systemd")
    container_path = os.path.join(systemd_dir, EMBED_CONTAINER)
    unit_text = render_embed_container_unit(data_dir, gpu, profile)
    existing = ""
    if os.path.exists(container_path):
        try:
            with open(container_path, encoding="utf-8") as f:
                existing = f.read()
        except OSError:
            existing = ""
    if existing != unit_text:
        try:
            os.makedirs(systemd_dir, exist_ok=True)
            with open(container_path, "w", encoding="utf-8") as f:
                f.write(unit_text)
            os.chmod(container_path, 0o644)
        except OSError as e:
            jlog(
                "error",
                "llama:embed",
                "could not write llama-embed.container",
                path=container_path,
                error=str(e),
            )
            return False
        subprocess.run(
            ["systemctl", "--user", "daemon-reload"], check=False, capture_output=True
        )
    started = subprocess.run(
        [
            "systemctl",
            "--user",
            "restart" if existing != unit_text else "start",
            EMBED_UNIT,
        ],
        capture_output=True,
        text=True,
    )
    if started.returncode != 0:
        jlog(
            "error",
            "llama:embed",
            "could not start the embeddings server",
            unit=EMBED_UNIT,
            stderr=started.stderr[:400],
        )
        return False
    jlog(
        "info",
        "llama:embed",
        "embeddings server running",
        unit=EMBED_UNIT,
        port=profile["port"],
        model=profile["model_file"],
    )
    return True


def embed_reachable(port: str) -> bool:
    """True when the embeddings server answers a real `/v1/embeddings` call.

    `/health` only says the model loaded; it does not say the server was
    started with `--embeddings`, and without that flag every embed request
    comes back 501 while the unit looks perfectly healthy.
    """
    status, _ = http_request(
        f"http://127.0.0.1:{port}/v1/embeddings",
        payload={"input": "ok"},
        method="POST",
        timeout=30,
    )
    return status == 200


# --- The whole-card GPU lease (#1320) -------------------------------------
#
# Box-measured over the night of 04./05.09. (#1318): the coding run's Qwen 27B
# peaks at 15 004 MiB of 16 380 — it does not fit beside Solaris' own e4b
# server (3 866 MiB), let alone the voice stack. The operator's decision is
# that such a job takes the card on request, with no time window and no
# presence check, and that Solaris answers honestly meanwhile.
#
# So a lease is: write the file, stop everything that holds VRAM. And a
# release is the same in reverse, with the file removed last — while it is
# there `solaris_chat.gpu_lease` makes the Engine say it is busy instead of
# talking into a dead socket.
#
# Since #1416 a *named* mode stops none of that on the llama side: the router
# holds all four presets and loads one at a time, so the mode sets the
# environment and writes `allowed` — the presets a client may ask for while it
# stands. Only the exclusive lease still empties the card.
LEASE_SCRIPT = "gpu-lease.py"
LEASE_FILE = "gpu_lease.json"
PROFILE_FILE = "llama-profile.json"

# The neighbour-service front for all of this (#1333, contract
# mdopp/foundry-chronicle#321). foundry asks over HTTP — `POST
# /api/model-lease` on the Engine — because that is the only door a container
# on this box can reach: it has no systemd, no `gpu-lease.py` and no way to
# restart llama.service. So the Engine writes what it wants into
# `gpu_lease_request.json`, this script's `.path` unit notices the write, and
# the `broker` verb below runs the very same `acquire`/`release` a human would
# type. The answer goes back through `gpu_lease_status.json`.
LEASE_REQUEST_FILE = "gpu_lease_request.json"
LEASE_STATUS_FILE = "gpu_lease_status.json"
BROKER_UNIT = "solaris-gpu-lease-broker"
SYSTEMD_USER_DIR = "~/.config/systemd/user"

# The batch transcriber and the wakeword trainer are background GPU jobs with
# no resident waiting on them, so a focus mode stops them for its window.
#
# The embeddings server (#1332) is NOT among them any more (operator,
# 2026-09-13). It costs ~300 MiB and both focus peaks leave more than that:
# the MoE at 131k takes 15 620 of 16 380 MiB, and the 27B with `-ctv q4_0
# -ub 256` at 82k about 15 300 — its 104k cell fitted in 15 724. Stopping it
# cost the household its semantic vault search for the whole window, which is
# a worse trade than 300 MiB.
#
# The two voice units are listed apart because the coding lease (#1319) keeps
# them RUNNING, on the CPU: the operator ruled on 2026-09-05 that the house can
# still be spoken to during a coding window, slower rather than not at all. The
# thinking mode (#1416) is the same shape. A foundry lease (#1325) stops
# nothing at all and leaves everything on the GPU.
LEASE_GPU_UNITS = (
    "solaris-whisper-batch.service",
    "solaris-wakeword-trainer.service",
)
LEASE_VOICE_UNITS = (
    "solaris-whisper.service",
    "solaris-tts.service",
)
# Only the exclusive lease empties the card, and that one takes the embeddings
# server with it.
LEASED_UNITS = LEASE_GPU_UNITS + LEASE_VOICE_UNITS + (EMBED_UNIT, "llama.service")

# Which execution provider the two voice units use, read from this file by
# their Quadlets (`EnvironmentFile=`). The other half of this contract is
# `templates/solaris/post-deploy.py`'s VOICE_DEVICE_* — the file is written
# there at install and flipped here for the duration of a coding lease;
# templates/tests/test_gpu_lease.py pins the two halves together.
VOICE_DEVICE_FILE = "voice-device.env"
VOICE_DEVICE_ENV = {
    "gpu": "WHISPER_DEVICE=cuda\nKOKORO_ONNX_PROVIDER=cuda\n",
    "cpu": "WHISPER_DEVICE=cpu\nKOKORO_ONNX_PROVIDER=cpu\n",
}

# The coding preset (#1319, re-measured #1415). Box-measured 2026-09-13 on
# image b10920: `-ctv q4_0` costs 3% of prompt processing rather than the
# 5-8x #1321 measured on the older image, and the 640 MiB it frees pay for
# `--spec-draft-n-max 8` — 44.7 tok/s against 38.7 at the unchanged 82k
# window, 12/12 tool calls, 15 652 of 16 380 MiB. `-ub 256` takes another
# 168 MiB for 5% of prefill. `--parallel 1` and q8 K are not tuning: with
# llama-server's stock 4 slots or f16 KV the drafter never loads at all.
#
# No `--reasoning off` any more (#1416): thinking is a per-request switch the
# client sends (`chat_template_kwargs.enable_thinking`), box-verified to work
# per request in router mode. A client that sends nothing gets a thinking
# trace and no tool call — that is now the client's setting to make, not the
# server's, because one server serves four presets at once.
CODING_PROFILE = {
    "model_repo": "unsloth/Qwen3.8-27B-GGUF",
    "model_file": "Qwen3.8-27B-UD-IQ3_XXS.gguf",
    "draft_repo": "ggml-org/Qwen3.8-27B-GGUF",
    "draft_file": "mtp-Qwen3.8-27B-Q4_0.gguf",
    "mmproj_file": "",
    "context_length": "81920",
    "draft_n_max": "8",
    "cache_type_k": "q8_0",
    "cache_type_v": "q4_0",
    "ubatch": "256",
    "parallel": "1",
    "alias": "qwen3.8-27b",
    "label": "Qwen 3.8 27B",
}

# The thinking preset (#1416, measured on #1418): Qwen 3.6 35B-A3B, a MoE with
# 3 of 35 B parameters active and only 10 of its 40 layers carrying KV. At
# 131 072 with q8 K+V it peaks at 15 620 of 16 380 MiB and runs 105 tok/s with
# 83.5% drafter acceptance, 12/12 tool calls, and found a planted sentence in
# an 85 287-token prompt. That is +171% generation and 3.3x prefill against
# the 27B, which is why reading and thinking moved here.
#
# No mmproj: vision was measured only to 98k and the operator scoped this
# preset to the 131k text window (#1416). `--parallel 1` as for the 27B.
THINKING_PROFILE = {
    "model_repo": "unsloth/Qwen3.6-35B-A3B-GGUF",
    "model_file": "Qwen3.6-35B-A3B-UD-IQ3_XXS.gguf",
    "draft_repo": "ggml-org/Qwen3.6-35B-A3B-GGUF",
    "draft_file": "mtp-Qwen3.6-35B-A3B-Q4_0.gguf",
    "mmproj_file": "",
    "context_length": "131072",
    "draft_n_max": "4",
    "cache_type_k": "q8_0",
    "cache_type_v": "q8_0",
    "ubatch": "",
    "parallel": "1",
    "alias": "qwen3.6-35b-a3b",
    "label": "Qwen 3.6 35B-A3B",
}

# The foundry preset (#1325, window re-measured #1415). 131 072 with q8 K+V
# fits in 10 156 MiB — 520 MiB more than the 32k f16 cell #1318 measured, and
# it carried a 85k prompt at 686 tok/s with 12/12 tool calls.
# No mmproj: the 12B repo's vision projector has never been fetched or
# measured on this box. A photo reaches the 12B as text for the window.
FOUNDRY_PROFILE = {
    "model_repo": "ggml-org/gemma-4-12B-it-GGUF",
    "model_file": "gemma-4-12B-it-Q4_0.gguf",
    "draft_repo": "ggml-org/gemma-4-12B-it-GGUF",
    "draft_file": "mtp-gemma-4-12B-it-Q8_0.gguf",
    "mmproj_file": "",
    "context_length": "131072",
    "draft_n_max": "4",
    "cache_type_k": "q8_0",
    "cache_type_v": "q8_0",
    "ubatch": "",
    "parallel": "",
    "alias": "gemma-4-12b",
    "label": "Gemma 4 12B",
}

# The two lease modes (#1435, operator 2026-09-19). The only question the
# policy actually has to answer is whether the card may serve something other
# than the household — which model that then is, the client decides with the
# `model` field of its request, and the router loads it on demand.
#
#   haushalt  — the card is the house's: voice stack on the GPU, embeddings
#               server up, and the household preset the only one a client may
#               ask for. It IS the absence of a lease, so it is never written
#               to disk; the proxy reports it when no lease stands.
#   erweitert — the card is free for something bigger: voice stack on the CPU,
#               embeddings server down, and every preset the router knows
#               allowed. `--models-max 1` swaps on demand, box-measured.
#
# `foundry`, `thinking` and `coding` are gone as modes: what separated them was
# only the allowed preset set, the environment was identical in all three. They
# live on as ALIASES of `erweitert` (below), so foundry-chronicle#321 and every
# other caller keeps working unchanged. The preset DEFINITIONS above stay —
# they describe weights, window and drafter and are still what presets.ini is
# rendered from.
#
# The embeddings server goes down in `erweitert` (operator 2026-09-19, measured
# in #1434): the MoE plus its MTP drafter needs 15 620 of 16 380 MiB and the
# box OOM'd the drafter's compute buffer by 168 MiB with the embeddings
# server's ~430 MiB resident. That the household loses semantic vault search
# for the window is now part of the decision rather than a broken promise.
# Without `--model` the lease is still exclusive: everything stops and nothing
# answers.
HOUSEHOLD_MODE = "haushalt"
EXTENDED_MODE = "erweitert"

LEASE_PROFILES = {
    EXTENDED_MODE: {
        "label": "Erweitert",
        "voice": "cpu",
        "stop_gpu_units": True,
        "stop_embed": True,
    },
}

# The old mode names, kept as aliases so nothing that still sends them breaks.
LEASE_MODE_ALIASES = {
    "foundry": EXTENDED_MODE,
    "thinking": EXTENDED_MODE,
    "coding": EXTENDED_MODE,
}

# And the preset each of those names has always meant: a caller that asks for
# `foundry` is still answered by the 12B, which is what `alias` in the lease
# file (contract #1333, foundry-chronicle#321) promises it.
ALIAS_PRESETS = {
    "foundry": FOUNDRY_PROFILE,
    "thinking": THINKING_PROFILE,
    "coding": CODING_PROFILE,
}


def canonical_mode(name: object) -> str:
    """The mode a name stands for today — the three retired ones map onto
    `erweitert` (#1435), everything else is itself."""
    text = str(name or "").strip()
    return LEASE_MODE_ALIASES.get(text, text)


def allowed_presets(mode: str) -> tuple[str, ...]:
    """The presets a client may ask the router for in `mode`.

    `erweitert` allows every preset the router knows — the client picks and the
    router swaps. Anything else is the household's own one.
    """
    if mode == EXTENDED_MODE:
        return tuple(preset_profiles())
    return (env_profile()["alias"],)


# How long `release` waits for the household model to answer /health again.
# Cold e4b was ~38 s in the night measurements; this is the give-up point,
# after which the lease file is dropped anyway rather than muting Solaris.
LEASE_WARM_DEADLINE_SEC = 300

# Every lease carries a deadline (#1319, precedent #1260): an end signal alone
# is not enough, because a coding run that dies without releasing would leave
# the household on the coding model — or, in exclusive mode, mute — until
# someone notices. A transient systemd timer runs `release` at the deadline.
LEASE_DEFAULT_DURATION_SEC = 4 * 3600
LEASE_EXPIRY_UNIT = "solaris-gpu-lease-expiry"

# The deadline alone was too coarse a net (#1361). A holder is expected to
# POST again every `renew_after` seconds; the timer is therefore armed at the
# **grace** — two missed renewals — instead of at the deadline, and every
# renewal re-arms it. So a holder that dies without a DELETE loses the card
# after two missed renewals (10 minutes on a 15-minute window) rather than
# after the full TTL, which for pi-web's 4-hour coding window would have kept
# Qwen loaded and the household on the wrong model for hours.
#
# The timer firing *is* the check: a live holder has cancelled and re-armed it
# at its last renewal, so nothing that reaches `release` here has renewed
# inside the grace. The deadline stays the outer net — `expiry_wake` never
# arms past it.
LEASE_GRACE_FACTOR = 2


def lease_file(data_dir: str) -> str:
    """The lease file, on the volume the chat pod mounts at /var/lib/solaris."""
    return os.path.join(data_dir, "solarisbay", LEASE_FILE)


def profile_file(data_dir: str) -> str:
    return os.path.join(data_dir, "solarisbay", PROFILE_FILE)


def presets_file(data_dir: str) -> str:
    return os.path.join(data_dir, "llama", "models", PRESETS_FILE)


def request_file(data_dir: str) -> str:
    return os.path.join(data_dir, "solarisbay", LEASE_REQUEST_FILE)


def status_file(data_dir: str) -> str:
    return os.path.join(data_dir, "solarisbay", LEASE_STATUS_FILE)


def voice_device_file(data_dir: str) -> str:
    return os.path.join(data_dir, "solarisbay", VOICE_DEVICE_FILE)


def save_household_profile(data_dir: str) -> None:
    """Record the installed household profile, so a `release` restores what the
    operator actually deployed instead of this script's own defaults."""
    path = profile_file(data_dir)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(env_profile(), f)
    except OSError as e:
        jlog(
            "warn",
            "llama:lease",
            "could not record the household profile",
            path=path,
            error=str(e),
        )


def household_profile(data_dir: str) -> dict[str, str]:
    """The profile `release` reloads: the recorded one, else this script's
    defaults (which is what a box installed before #1319 has)."""
    profile = env_profile()
    try:
        with open(profile_file(data_dir), encoding="utf-8") as f:
            saved = json.load(f)
    except (OSError, ValueError):
        return profile
    if isinstance(saved, dict):
        profile.update({k: str(v) for k, v in saved.items() if k in profile})
    return profile


def parse_duration(text: str) -> int:
    """`4h` / `90m` / `3600` → seconds. 0 for anything unreadable."""
    text = text.strip().lower()
    factor = 1
    if text.endswith("h"):
        factor, text = 3600, text[:-1]
    elif text.endswith("m"):
        factor, text = 60, text[:-1]
    elif text.endswith("s"):
        text = text[:-1]
    try:
        seconds = int(float(text) * factor)
    except ValueError:
        return 0
    return seconds if seconds > 0 else 0


def read_lease(data_dir: str) -> dict[str, object]:
    """The lease as it stands, with a retired mode name migrated (#1435).

    A box upgraded mid-window has `foundry`/`thinking`/`coding` on disk. Read
    as-is, that mode matches no profile any more: the release would put neither
    the voice stack back on the GPU nor the embeddings server back up, and the
    deploy would keep reporting a mode nothing knows.
    """
    try:
        with open(lease_file(data_dir), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    if data.get("mode") in LEASE_MODE_ALIASES:
        data["mode"] = EXTENDED_MODE
    return data


def systemctl(verb: str, units: tuple[str, ...]) -> bool:
    out = subprocess.run(
        ["systemctl", "--user", verb, *units],
        capture_output=True,
        text=True,
    )
    if out.returncode != 0:
        jlog(
            "warn",
            "llama:lease",
            f"systemctl {verb} reported a failure",
            units=list(units),
            stderr=out.stderr[:400],
        )
    return out.returncode == 0


def write_lease(data_dir: str, record: dict[str, object]) -> bool:
    # Written aside and renamed into place: the policy proxy notes the preset
    # it served here (#1435) while the Engine reads the file on every turn, and
    # a half-written lease reads as "held, not ready" — the busy sentence.
    path = lease_file(data_dir)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(f"{path}.tmp", "w", encoding="utf-8") as f:
            json.dump(record, f)
        os.chmod(f"{path}.tmp", 0o644)
        os.replace(f"{path}.tmp", path)
    except OSError as e:
        jlog(
            "error", "llama:lease", "could not write the lease", path=path, error=str(e)
        )
        return False
    return True


def set_voice_device(data_dir: str, device: str) -> None:
    """Put the two voice units on `cuda` or `cpu` and restart them.

    An env file rather than a second pair of units: the household's whisper
    model, prompt, health probe and Wyoming ports are one definition either way,
    and only the execution provider moves (#1319)."""
    path = voice_device_file(data_dir)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(VOICE_DEVICE_ENV[device])
        os.chmod(path, 0o644)
    except OSError as e:
        jlog(
            "error",
            "llama:lease",
            "could not switch the voice units; leaving them as they are",
            path=path,
            device=device,
            error=str(e),
        )
        return
    systemctl("restart", LEASE_VOICE_UNITS)
    jlog(
        "info",
        "llama:lease",
        f"voice stack switched to {device}",
        units=list(LEASE_VOICE_UNITS),
    )


def warm_preset(llama_url: str, preset: str, deadline_sec: int) -> bool:
    """Ask the router for one token from `preset`, so it is loaded.

    The router loads on demand and a cold preset costs 9-19 s (#1415) — after
    a release that wait would land on the next resident instead of here. This
    doubles as the readiness probe: it only answers once the child process
    serving `preset` is up, which `/health` on the router does not say.
    """
    started = time.time()
    last_beat = 0.0
    while time.time() - started < deadline_sec:
        status, _ = http_request(
            f"{llama_url}/v1/chat/completions",
            payload={
                "model": preset,
                "messages": [{"role": "user", "content": "ok"}],
                "max_tokens": 1,
            },
            method="POST",
            timeout=120,
        )
        if status == 200:
            return True
        elapsed = time.time() - started
        if elapsed - last_beat >= 10:
            jlog(
                "info",
                "llama:warm",
                "still waiting for the preset to load",
                preset=preset,
                elapsed_sec=int(elapsed),
            )
            last_beat = elapsed
        time.sleep(3)
    return False


def renew_after(ttl: int) -> int:
    """When the holder is expected to POST again — a third of the window, so a
    missed renewal has two more chances. The same arithmetic the Engine
    answers in `renew_after` (`solaris_chat.model_lease`)."""
    return max(ttl // 3, 60)


def expiry_wake(ttl: int) -> int:
    """When the expiry timer wakes: after the grace of two missed renewals, or
    at the deadline when that comes first (#1361)."""
    return min(LEASE_GRACE_FACTOR * renew_after(ttl), ttl)


def schedule_expiry(data_dir: str, port: str, seconds: int) -> None:
    """Arm the transient timer that gives the card back once the holder has
    stopped renewing (or the window has run out, whichever is first)."""
    cancel_expiry()
    out = subprocess.run(
        [
            "systemd-run",
            "--user",
            f"--unit={LEASE_EXPIRY_UNIT}",
            f"--on-active={expiry_wake(seconds)}",
            "--description=Solaris GPU lease expiry (#1319)",
            f"--setenv=DATA_DIR={data_dir}",
            f"--setenv=LLAMA_ROUTER_PORT={port}",
            sys.executable,
            os.path.realpath(__file__),
            "release",
        ],
        capture_output=True,
        text=True,
    )
    if out.returncode != 0:
        jlog(
            "error",
            "llama:lease",
            "could not arm the expiry timer — release the card by hand when the job is done",
            stderr=out.stderr[:400],
        )
        return
    jlog(
        "info",
        "llama:lease",
        "expiry armed",
        seconds=expiry_wake(seconds),
        ttl_s=seconds,
        renew_after_s=renew_after(seconds),
    )


def cancel_expiry() -> None:
    subprocess.run(
        ["systemctl", "--user", "stop", f"{LEASE_EXPIRY_UNIT}.timer"],
        check=False,
        capture_output=True,
    )


def lease_acquire(
    data_dir: str,
    holder: str,
    port: str = "11434",
    model: str = "",
    duration_sec: int = LEASE_DEFAULT_DURATION_SEC,
) -> int:
    """Hand the card to `holder`: claim, then set the environment.

    `model=erweitert` (#1435) is the softer variant, and since #1416 it does
    not touch llama-server at all: the router serves every preset and the mode
    only decides what the environment looks like and which presets a client may
    ask for. It stops the batch transcriber, the wakeword trainer and the
    embeddings server and moves the voice stack to the CPU — the house is still
    spoken to, slower rather than not at all. `foundry`, `thinking` and
    `coding` are accepted as the old names of the same mode and decide only
    which preset the holder is told it will be answered by. Without `--model`
    the card is emptied outright.
    """
    current = read_lease(data_dir)
    if current and current.get("holder") != holder:
        jlog(
            "error",
            "llama:lease",
            "the card is already leased; release it first",
            holder=current.get("holder"),
            requested_by=holder,
        )
        return 1
    mode = canonical_mode(model)
    if model and mode not in LEASE_PROFILES:
        jlog(
            "error",
            "llama:lease",
            "unknown --model; known: "
            + ", ".join(sorted({*LEASE_PROFILES, *LEASE_MODE_ALIASES})),
            model=model,
        )
        return 2
    profile = LEASE_PROFILES.get(mode)
    # The preset the requested name promises the holder, empty when it asked
    # for the mode itself — then the door fills it in from what is actually
    # served (`note_preset`).
    wanted = ALIAS_PRESETS.get(str(model).strip(), {})
    # A renewal (#1333): the same holder asking again for the mode it already
    # has moves the deadline, it does not swap the server a second time — a
    # restart every renewal interval would rebuild exactly the thrash the lease
    # exists to prevent. Compared on the canonical mode, so a holder renewing
    # under an old name (pi-web sends `coding`) renews instead of re-running
    # the whole environment switch on every heartbeat.
    if profile and canonical_mode(current.get("mode")) == mode and current.get("ready"):
        current["until"] = time.time() + duration_sec
        current["last_renewed_at"] = time.time()
        current["renew_after"] = renew_after(duration_sec)
        write_lease(data_dir, current)
        schedule_expiry(data_dir, port, duration_sec)
        jlog(
            "info",
            "llama:lease",
            "lease renewed",
            holder=holder,
            model=profile["label"],
            until_sec=int(current["until"]),
        )
        return 0
    if profile:
        # Before anything stops: 13 GB over a household line is not something
        # to do with the house muted, and a second acquire finds the files.
        if not ensure_preset_weights(data_dir, allowed_presets(mode)):
            jlog(
                "error",
                "llama:lease",
                f"the {mode} weights are not on the box; nothing was stopped",
            )
            return 1
    now = time.time()
    if not write_lease(
        data_dir,
        {
            "holder": holder,
            "since": now,
            "until": now + duration_sec,
            # The heartbeat the expiry timer is armed against (#1361): moved
            # by every renewal, reported by `GET /api/model-lease` so a holder
            # can see how long its window survives its own silence.
            "last_renewed_at": now,
            "renew_after": renew_after(duration_sec),
            "mode": mode or "exclusive",
            # The model the holder will be answered by, named for a human. In
            # `erweitert` nobody has chosen one yet, so it stays empty until
            # the door knows better.
            "model": wanted.get("label", ""),
            # What llama-server answers as for the window — solaris-chat hands
            # this straight to the lease holder (#1333). In `erweitert` the
            # client picks its own preset, so this starts empty and the policy
            # proxy keeps it on whatever is actually being served (#1435).
            "alias": wanted.get("alias", "") if profile else "",
            # The mode policy (#1416): the presets a client may ask the router
            # for while this lease stands. The router itself has no policy —
            # this is what the Engine and the HTTP lease layer refuse against,
            # so a request for a preset outside the mode is answered with the
            # mode's name instead of evicting the household model.
            "allowed": list(allowed_presets(mode)) if profile else [],
            # Flipped once the mode's environment is set. An exclusive lease
            # leaves it false: nothing is serving, and the Engine says so.
            "ready": False,
        },
    ):
        return 1
    # Claim before stopping: in the gap Solaris already says it is busy. The
    # other order leaves a window where the server is gone and nothing knows.
    schedule_expiry(data_dir, port, duration_sec)
    if not profile:
        systemctl("stop", LEASED_UNITS)
        jlog(
            "info",
            "llama:lease",
            "GPU leased — voice stack, embeddings server and llama-server stopped",
            holder=holder,
            units=list(LEASED_UNITS),
            until_sec=int(now + duration_sec),
        )
        return 0
    if profile["stop_gpu_units"]:
        systemctl("stop", LEASE_GPU_UNITS)
    if profile["stop_embed"]:
        systemctl("stop", (EMBED_UNIT,))
    if profile["voice"] == "cpu":
        set_voice_device(data_dir, "cpu")
    # No restart: llama-server keeps serving every preset and loads the one the
    # holder asks for on its first request (#1416). The card is free of the
    # household model as soon as that happens — the router evicts the idle LRU
    # child rather than holding two.
    current = read_lease(data_dir)
    current["ready"] = True
    write_lease(data_dir, current)
    jlog(
        "info",
        "llama:lease",
        f"GPU leased for {mode} — Solaris keeps answering, from the presets this mode allows",
        holder=holder,
        model=wanted.get("label", profile["label"]),
        allowed=list(allowed_presets(mode)),
        voice=profile["voice"],
        until_sec=int(now + duration_sec),
    )
    return 0


def lease_release(data_dir: str, port: str) -> int:
    """Give the card back: start everything, warm the household preset, drop
    the lease last so nobody is told "ready" while e4b is still loading."""
    mode = canonical_mode(read_lease(data_dir).get("mode"))
    cancel_expiry()
    profile = LEASE_PROFILES.get(mode)
    if profile:
        if profile["stop_gpu_units"]:
            systemctl("start", LEASE_GPU_UNITS)
        if profile["stop_embed"]:
            systemctl("start", (EMBED_UNIT,))
        if profile["voice"] == "cpu":
            set_voice_device(data_dir, "gpu")
    else:
        systemctl("start", LEASED_UNITS)
    # The router still has the leased preset resident. Asking it for the
    # household one now pays the 9-19 s load here instead of on the next
    # resident's turn (#1415).
    llama_url = f"http://127.0.0.1:{port}"
    warm = warm_preset(
        llama_url, household_profile(data_dir)["alias"], LEASE_WARM_DEADLINE_SEC
    )
    try:
        os.unlink(lease_file(data_dir))
    except OSError:
        pass
    if not warm:
        jlog(
            "warn",
            "llama:lease",
            "units restarted but the household preset did not answer; the lease is cleared anyway so Solaris stops saying it is busy. Check `journalctl --user -u llama.service`.",
            url=llama_url,
        )
        return 1
    jlog("info", "llama:lease", "GPU released — household model warm again")
    return 0


def read_request(data_dir: str) -> dict[str, object]:
    try:
        with open(request_file(data_dir), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_status(data_dir: str, record: dict[str, object]) -> None:
    path = status_file(data_dir)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(record, f)
        os.chmod(path, 0o644)
    except OSError as e:
        jlog(
            "error",
            "llama:broker",
            "could not write the lease status",
            path=path,
            error=str(e),
        )


def broker_run(data_dir: str, port: str) -> int:
    """Apply the request the Engine wrote, and answer in the status file.

    The `requested_at` is echoed back unchanged: that is how the HTTP side
    tells a request still waiting for this unit from one it has already
    handled, without either side keeping a clock.
    """
    request = read_request(data_dir)
    if not request:
        return 0
    op = request.get("op")
    requested_at = request.get("requested_at")
    holder = str(request.get("holder") or "")
    if op == "release":
        rc = lease_release(data_dir, port)
        write_status(
            data_dir,
            {
                "requested_at": requested_at,
                "op": "release",
                "state": "released",
                "model": "",
                "holder": holder,
                "alias": household_profile(data_dir)["alias"],
                "expires_at": None,
                "error": "" if rc == 0 else "llama-server did not come back",
            },
        )
        return 0
    model = str(request.get("model") or "")
    profile = LEASE_PROFILES.get(canonical_mode(model))
    if op != "acquire" or profile is None:
        write_status(
            data_dir,
            {
                "requested_at": requested_at,
                "op": op,
                "state": "error",
                "model": model,
                "holder": holder,
                "alias": household_profile(data_dir)["alias"],
                "expires_at": None,
                "error": "unknown request",
            },
        )
        return 0
    ttl = int(request.get("ttl_s") or LEASE_DEFAULT_DURATION_SEC)
    holder = holder or model
    rc = lease_acquire(data_dir, holder, port, model, ttl)
    lease = read_lease(data_dir)
    ready = rc == 0 and bool(lease.get("ready"))
    write_status(
        data_dir,
        {
            "requested_at": requested_at,
            "op": "acquire",
            "state": "ready" if ready else "error",
            "model": model,
            "holder": holder,
            # The preset the window will be answered by: the one the requested
            # name promises, else — in `erweitert`, where the client picks —
            # whatever is loaded right now, which is the household's until a
            # client asks for something else (#1435).
            "alias": str(lease.get("alias") or household_profile(data_dir)["alias"])
            if ready
            else household_profile(data_dir)["alias"],
            "expires_at": lease.get("until") if ready else None,
            "error": "" if ready else "the lease could not be taken",
        },
    )
    return 0


def render_broker_units(data_dir: str, port: str, script: str) -> tuple[str, str]:
    """The `.path`/`.service` pair, pure so the test can read them.

    A path unit rather than a socket or a poll: the request file is on the
    volume the chat pod already mounts, so the write itself is the signal and
    nothing has to be exposed to the container.
    """
    path_unit = (
        "[Unit]\n"
        "Description=Watch for a Solaris GPU lease request (#1333)\n"
        "\n"
        "[Path]\n"
        f"PathChanged={request_file(data_dir)}\n"
        f"Unit={BROKER_UNIT}.service\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )
    service_unit = (
        "[Unit]\n"
        "Description=Apply a Solaris GPU lease request (#1333)\n"
        "\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"Environment=DATA_DIR={data_dir}\n"
        f"Environment=LLAMA_ROUTER_PORT={port}\n"
        # The first foundry lease downloads 8 GB before it swaps anything.
        "TimeoutStartSec=3600\n"
        f"ExecStart={sys.executable} {script} broker\n"
    )
    return path_unit, service_unit


def install_broker_units(data_dir: str, port: str, script: str) -> None:
    """Write + enable the request watcher. Idempotent: same text, same enable."""
    if not script:
        return
    unit_dir = os.path.expanduser(SYSTEMD_USER_DIR)
    path_unit, service_unit = render_broker_units(data_dir, port, script)
    try:
        os.makedirs(unit_dir, exist_ok=True)
        os.makedirs(os.path.dirname(request_file(data_dir)), exist_ok=True)
        for name, text in (
            (f"{BROKER_UNIT}.path", path_unit),
            (f"{BROKER_UNIT}.service", service_unit),
        ):
            with open(os.path.join(unit_dir, name), "w", encoding="utf-8") as f:
                f.write(text)
            os.chmod(os.path.join(unit_dir, name), 0o644)
    except OSError as e:
        jlog(
            "error",
            "llama:broker",
            "could not install the lease broker; foundry's HTTP lease will not switch anything",
            path=unit_dir,
            error=str(e),
        )
        return
    subprocess.run(
        ["systemctl", "--user", "daemon-reload"], check=False, capture_output=True
    )
    systemctl("enable", ("--now", f"{BROKER_UNIT}.path"))
    jlog("info", "llama:broker", "lease broker installed", unit=f"{BROKER_UNIT}.path")


def lease_cli(argv: list[str]) -> int:
    data_dir = env("DATA_DIR", "/mnt/data/stacks")
    # The router, not the door: `release` warms the household preset while the
    # lease file still stands, which the policy proxy on LLAMA_PORT would be
    # right to refuse (#1416).
    port = env("LLAMA_ROUTER_PORT", "11434")
    if argv[0] != "acquire":
        return lease_release(data_dir, port)
    holder, model, duration = "", "", LEASE_DEFAULT_DURATION_SEC
    rest = argv[1:]
    while rest:
        token = rest.pop(0)
        if token in ("--model", "--duration"):
            value = rest.pop(0) if rest else ""
            if token == "--model":
                model = value.strip()
            else:
                duration = parse_duration(value)
        elif not holder:
            holder = token.strip()
    if not holder or not duration:
        jlog(
            "error",
            "llama:lease",
            "usage: gpu-lease.py acquire <holder> [--model erweitert] [--duration 4h]",
        )
        return 2
    return lease_acquire(data_dir, holder, port, model, duration)


def install_lease_script(data_dir: str) -> str:
    """Copy this script to a durable path so foundry and the coding run can
    call it. Same self-copy as ollama-warm (#1236): one source of truth for
    the unit list, and no second file to fall out of step with it."""
    dst = os.path.join(data_dir, "solarisbay", LEASE_SCRIPT)
    try:
        with open(os.path.realpath(__file__), encoding="utf-8") as f:
            self_src = f.read()
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(dst, "w", encoding="utf-8") as f:
            f.write(self_src)
        os.chmod(dst, 0o755)
    except OSError as e:
        jlog(
            "warn",
            "llama:lease",
            "could not install the gpu-lease script",
            path=dst,
            error=str(e),
        )
        return ""
    jlog("info", "llama:lease", "gpu-lease installed", path=dst)
    return dst


# --- The mode policy proxy (#1416) ----------------------------------------
#
# The router polices nothing: asked for a preset, it loads it. With
# `--models-max 1` that evicts whatever was resident, so one client on
# LLAMA_PORT asking for the 27B during a household evening costs the next
# resident turn a 10-20 s reload — precisely what the operator took the mode
# for. The Engine refusing it on its own side does not help, because the
# clients that do this (PI WEB, aider, goose, Continue) never pass through the
# Engine.
#
# So the router moved to LLAMA_ROUTER_PORT on loopback and this proxy holds
# LLAMA_PORT instead, with the same wide bind and the same `blockLanAccess`
# firewall rule the router used to have. Per request it reads the lease's
# `allowed` set, answers 409 for a preset outside it, marks `/v1/models` with
# which presets that set holds (#1431) and forwards everything else verbatim.
#
# It is a verb of this script rather than a file of its own: the copy
# `install_lease_script` already puts on the box carries the preset table, the
# lease reader and the household default, and a second copy of those is
# exactly what drifts.
POLICY_UNIT = "solaris-llama-policy"

# Big enough that a whole SSE frame usually arrives in one write, small enough
# that a long answer is never held back waiting to fill it.
PROXY_CHUNK = 64 * 1024

# Between the router's slowest honest answer (a 51 s cold load plus a long
# generation) and a hung socket. The Engine gives up at 300 s of its own.
PROXY_TIMEOUT_SEC = 600

# RFC 9110: these describe the one hop and must not be relayed. `content-length`
# is re-derived rather than copied, because the body may be rewritten.
HOP_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)

MODELS_PATHS = ("/v1/models", "/models")


def proxy_policy(data_dir: str) -> tuple[list[str], str]:
    """`(presets a client may ask for, the mode that says so)`.

    Read from the lease file on every request rather than cached: the mode is
    taken and dropped from the phone, and the next request has to see it.
    """
    lease = read_lease(data_dir)
    if not lease:
        return [household_profile(data_dir)["alias"]], HOUSEHOLD_MODE
    allowed = [
        name.strip()
        for name in lease.get("allowed") or []
        if isinstance(name, str) and name.strip()
    ]
    return allowed, str(lease.get("mode") or "exclusive")


def note_preset(data_dir: str, preset: str) -> None:
    """Record in the lease which preset the door just served (#1435).

    In `erweitert` the client chooses the model, so this is the only place the
    Modell tile and the Engine can learn which one is loaded: without it the
    tile would go on naming the preset the lease was taken for, and the Engine
    would ask for the household one and evict what the holder is using.
    """
    lease = read_lease(data_dir)
    if not lease or lease.get("alias") == preset:
        return
    lease["alias"] = preset
    write_lease(data_dir, lease)


def requested_model(body: bytes) -> str:
    """The `model` field of a `/v1` request, `""` when it carries none."""
    try:
        request = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return ""
    if not isinstance(request, dict):
        return ""
    model = request.get("model")
    return model.strip() if isinstance(model, str) else ""


def denial(model: str, mode: str, allowed: list[str]) -> dict[str, object]:
    """The 409 body: what was refused, which mode refused it, what may be asked
    for instead, and where the remedy is. German, because the operator is who
    reads it — in PI WEB's ticket protocol, in aider's error line, in a log
    someone scrolls.

    With two modes (#1435) there is one refusal worth the name: `haushalt` lets
    only the household preset through, and the way out of that is the Modell
    tile. In `erweitert` every preset is allowed, so a refusal there can only
    be a name the router does not serve — a typo, not a policy.
    """
    if not allowed:
        say = "Die Grafikkarte ist exklusiv vergeben; es antwortet gerade kein Modell."
        remedy = "Die Modell-Kachel in Solaris zeigt, bis wann."
    elif len(allowed) == 1:
        say = f"Erlaubt ist: {allowed[0]}."
        remedy = (
            "Für die anderen Modelle in der Modell-Kachel in Solaris "
            "auf „Erweitert“ umschalten."
        )
    else:
        say = f"Erlaubt sind: {', '.join(allowed)}."
        remedy = "Eines davon im Feld `model` der Anfrage angeben."
    return {
        "error": {
            "message": f"Modell {model} ist im Modus {mode} nicht erlaubt. "
            f"{say} {remedy}",
            "mode": mode,
            "allowed": allowed,
        }
    }


def mark_models(body: bytes, allowed: list[str], mode: str) -> bytes:
    """`/v1/models` with every preset the router knows still listed, each one
    marked `allowed_in_mode` and the standing mode named at the top (#1431).

    Filtering the list to the mode hid the other three presets from anyone
    whose only door is this interface. The enforcement sits on the request,
    not on the list — a preset outside the mode still gets the 409 — so naming
    all four costs nothing.

    `allowed_in_mode` is a different axis from the router's own
    `status.value`: `unloaded` says the weights are not in VRAM, which for an
    allowed preset is normal and costs 7-17 s on the first turn. Every field
    the router sent is passed through untouched, so a client reading only `id`
    is unaffected.
    """
    try:
        listing = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return body
    if not isinstance(listing, dict) or not isinstance(listing.get("data"), list):
        return body
    listing["mode"] = mode
    listing["data"] = [
        {**entry, "allowed_in_mode": entry.get("id") in allowed}
        if isinstance(entry, dict)
        else entry
        for entry in listing["data"]
    ]
    return json.dumps(listing).encode("utf-8")


def make_proxy_server(
    data_dir: str, listen_port: int, router_port: int
) -> http.server.ThreadingHTTPServer:
    """The policy proxy, bound and ready to serve. Returned rather than run so
    the test drives the very object the `proxy` verb runs."""

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args: object) -> None:
            """A refusal gets a jlog line; a token stream does not get 400."""

        def do_GET(self) -> None:
            if self.path.split("?")[0] in MODELS_PATHS:
                self._catalogue()
                return
            self._forward(b"")

        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            allowed, mode = proxy_policy(data_dir)
            wanted = requested_model(body)
            # A request naming no model is forwarded: the router answers it
            # from the preset it already has resident, which cannot be one
            # outside the mode, so there is nothing here to refuse.
            if wanted and wanted not in allowed:
                jlog(
                    "info",
                    "llama:policy",
                    "refused a preset the standing mode does not allow",
                    model=wanted,
                    mode=mode,
                    allowed=allowed,
                )
                self._answer(
                    409, json.dumps(denial(wanted, mode, allowed)).encode("utf-8")
                )
                return
            if wanted:
                note_preset(data_dir, wanted)
            self._forward(body)

        def _upstream(self) -> dict[str, str]:
            return {
                key: value
                for key, value in self.headers.items()
                if key.lower() not in HOP_HEADERS
            }

        def _answer(self, status: int, payload: bytes) -> None:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            self.wfile.write(payload)

        def _catalogue(self) -> None:
            allowed, mode = proxy_policy(data_dir)
            conn = http.client.HTTPConnection(
                "127.0.0.1", router_port, timeout=PROXY_TIMEOUT_SEC
            )
            try:
                conn.request("GET", self.path, headers=self._upstream())
                response = conn.getresponse()
                status, body = response.status, response.read()
            except OSError:
                self._answer(502, self._unreachable())
                return
            finally:
                conn.close()
            self._answer(
                status, mark_models(body, allowed, mode) if status == 200 else body
            )

        def _forward(self, body: bytes) -> None:
            headers = self._upstream()
            if self.command == "POST":
                headers["Content-Length"] = str(len(body))
            conn = http.client.HTTPConnection(
                "127.0.0.1", router_port, timeout=PROXY_TIMEOUT_SEC
            )
            try:
                conn.request(
                    self.command, self.path, body=body or None, headers=headers
                )
                response = conn.getresponse()
            except OSError:
                conn.close()
                self._answer(502, self._unreachable())
                return
            try:
                self.send_response(response.status)
                for key, value in response.getheaders():
                    if (
                        key.lower() not in HOP_HEADERS
                        and key.lower() != "content-length"
                    ):
                        self.send_header(key, value)
                length = response.getheader("Content-Length")
                if length is not None:
                    self.send_header("Content-Length", length)
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True
                while True:
                    # read1, not read: `read(n)` on a chunked response keeps
                    # pulling chunks until it has n bytes, which would hold an
                    # SSE stream back until the whole answer is finished.
                    chunk = response.read1(PROXY_CHUNK)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except OSError:
                self.close_connection = True
            finally:
                conn.close()

        def _unreachable(self) -> bytes:
            allowed, mode = proxy_policy(data_dir)
            return json.dumps(
                {
                    "error": {
                        "message": "llama-server antwortet nicht. "
                        "`journalctl --user -u llama.service` sagt warum.",
                        "mode": mode,
                        "allowed": allowed,
                    }
                }
            ).encode("utf-8")

    server = http.server.ThreadingHTTPServer(("0.0.0.0", listen_port), Handler)
    server.daemon_threads = True
    return server


def proxy_run(data_dir: str, listen_port: str, router_port: str) -> int:
    server = make_proxy_server(data_dir, int(listen_port), int(router_port))
    jlog(
        "info",
        "llama:policy",
        "mode policy proxy serving",
        listen=int(listen_port),
        router=int(router_port),
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def render_policy_unit(
    data_dir: str, listen_port: str, router_port: str, script: str
) -> str:
    """The proxy's unit, pure so the test can read it."""
    return (
        "[Unit]\n"
        "Description=Solaris llama mode policy proxy (#1416)\n"
        "Wants=network-online.target\n"
        "After=network-online.target\n"
        "\n"
        "[Service]\n"
        f"Environment=DATA_DIR={data_dir}\n"
        f"Environment=LLAMA_PORT={listen_port}\n"
        f"Environment=LLAMA_ROUTER_PORT={router_port}\n"
        f"ExecStart={sys.executable} {script} proxy\n"
        "Restart=always\n"
        "RestartSec=5\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def install_policy_unit(
    data_dir: str, listen_port: str, router_port: str, script: str
) -> None:
    """Write, enable and restart the proxy.

    Restarted unconditionally rather than only on a changed unit file: the
    script it runs is rewritten by this same install, and a proxy still
    executing the previous copy would police the previous table.
    """
    if not script:
        jlog(
            "error",
            "llama:policy",
            "no lease script on the box, so no policy proxy — nothing would answer on LLAMA_PORT at all",
        )
        return
    unit_dir = os.path.expanduser(SYSTEMD_USER_DIR)
    path = os.path.join(unit_dir, f"{POLICY_UNIT}.service")
    try:
        os.makedirs(unit_dir, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(render_policy_unit(data_dir, listen_port, router_port, script))
        os.chmod(path, 0o644)
    except OSError as e:
        jlog(
            "error",
            "llama:policy",
            "could not install the policy proxy; nothing would answer on LLAMA_PORT",
            path=path,
            error=str(e),
        )
        return
    subprocess.run(
        ["systemctl", "--user", "daemon-reload"], check=False, capture_output=True
    )
    systemctl("enable", ("--now", f"{POLICY_UNIT}.service"))
    systemctl("restart", (f"{POLICY_UNIT}.service",))
    jlog(
        "info",
        "llama:policy",
        "mode policy proxy installed",
        unit=f"{POLICY_UNIT}.service",
        listen=listen_port,
        router=router_port,
    )


def register_http_check(
    sb_api: str,
    sb_token: str,
    llama_url: str,
    check_id: str = "llama-api",
    name: str = "llama.cpp API",
) -> None:
    """Best-effort: a non-200 here doesn't block the install."""
    headers = {}
    if sb_token:
        headers["X-SB-Internal-Token"] = sb_token
    status, body = http_request(
        f"{sb_api}/api/health/checks",
        payload={
            "id": check_id,
            "name": name,
            "type": "http",
            "target": f"{llama_url}/health",
            "interval": 60,
            "enabled": True,
            "httpConfig": {"expectedStatus": 200},
        },
        method="POST",
        timeout=10,
        extra_headers=headers,
    )
    if status == 200:
        jlog("info", "llama:health", f"registered http check {check_id}")
    else:
        jlog(
            "warn",
            "llama:health",
            "could not register http check",
            status=status,
            body=body.decode("utf-8", errors="replace")[:200],
        )


def main() -> int:
    # The lease entrypoint (#1320). Gated on the exact verb rather than on
    # "any argv", so a future ServiceBay that passes the script an argument
    # still installs instead of trying to move the GPU.
    if len(sys.argv) > 1 and sys.argv[1] in ("acquire", "release"):
        return lease_cli(sys.argv[1:])
    # The host broker (#1333): what `solaris-gpu-lease-broker.service` runs when
    # the Engine writes a request for a neighbour service.
    if len(sys.argv) > 1 and sys.argv[1] == "broker":
        return broker_run(
            env("DATA_DIR", "/mnt/data/stacks"), env("LLAMA_ROUTER_PORT", "11434")
        )
    # The mode policy on LLAMA_PORT (#1416), run by solaris-llama-policy.service
    # from the copy install_lease_script puts on the box.
    if len(sys.argv) > 1 and sys.argv[1] == "proxy":
        return proxy_run(
            env("DATA_DIR", "/mnt/data/stacks"),
            env("LLAMA_PORT", "11435"),
            env("LLAMA_ROUTER_PORT", "11434"),
        )

    port = env("LLAMA_PORT", "11435")
    router_port = env("LLAMA_ROUTER_PORT", "11434")
    repo = env("LLAMA_MODEL_REPO", "ggml-org/gemma-4-E4B-it-GGUF")
    stall_sec = int(env("LLAMA_DOWNLOAD_STALL_SECONDS", "600"))
    sb_api = env("SB_API_URL", "http://localhost:3000")
    sb_token = env("SB_API_TOKEN", "")
    data_dir = env("DATA_DIR", "/mnt/data/stacks")
    models_dir = os.path.join(data_dir, "llama", "models")
    # post-deploy talks to the router itself: it warms the household preset,
    # which the policy proxy is entitled to refuse mid-lease.
    llama_url = f"http://127.0.0.1:{router_port}"
    policy_url = f"http://127.0.0.1:{port}"

    _gpu = env("LLAMA_GPU_PASSTHROUGH", "").strip().lower()
    if _gpu in ("yes", "true", "1"):
        gpu_requested = True
    elif _gpu in ("no", "false", "0", "off"):
        gpu_requested = False
    else:
        gpu_requested = os.path.exists("/etc/cdi/nvidia.yaml")

    try:
        os.makedirs(models_dir, exist_ok=True)
    except OSError as e:
        jlog(
            "error",
            "llama:models",
            "could not create the model directory",
            path=models_dir,
            error=str(e),
        )
        return 0

    # Weights first: the container crash-loops until they exist, and the GPU
    # fixup below restarts it once — so a first install converges without
    # anyone waiting on a restart loop. All four presets (#1416), because the
    # router lists every one of them from the first start.
    ensure_preset_weights(data_dir, list(preset_profiles()))
    write_presets(data_dir)

    embed = embed_profile()
    if embed["port"] and not download_model(
        embed["model_repo"], embed["model_file"], models_dir, stall_sec
    ):
        jlog(
            "warn",
            "llama:embed",
            "the embedding weights are not on the box — the vault's semantic search stays on keyword hits until they are. Download %s from https://huggingface.co/%s into %s"
            % (embed["model_file"], embed["model_repo"], models_dir),
            file=embed["model_file"],
        )

    # A deploy in the middle of a lease must not take the card back, so the
    # household warm-up below is skipped — but the UNIT still has to converge.
    # It is mode-independent since #1416 (one router, four presets), and the
    # v2 -> v3 deploy proved what skipping it costs: the router stayed on the
    # v2 argv holding LLAMA_PORT, the policy proxy could not bind that port and
    # crash-looped 80 times, and nothing would have converged it, because a
    # lease no longer rewrites the unit either.
    leased = os.path.exists(lease_file(data_dir))
    lease_mode = str(read_lease(data_dir).get("mode", "")) if leased else ""

    if leased:
        jlog(
            "info",
            "llama:bootstrap",
            "a GPU lease is held; the units are converged but the household model is not warmed",
            holder=str(read_lease(data_dir).get("holder", "")),
            mode=lease_mode,
        )
    if gpu_requested:
        install_gpu_quadlet_fallback(router_port, data_dir)
        install_embed_unit(data_dir, gpu=True)
    else:
        install_embed_unit(data_dir, gpu=False)
        jlog(
            "info",
            "llama:bootstrap",
            "GPU passthrough not requested; llama-server runs on the CPU and will be slow",
        )
    # `install_embed_unit` starts the server; the one mode that cannot hold it
    # on the card has to get it stopped again, or the deploy leaves the MoE
    # unable to load for the rest of the window.
    if (LEASE_PROFILES.get(lease_mode) or {}).get("stop_embed"):
        systemctl("stop", (EMBED_UNIT,))
        jlog(
            "info",
            "llama:bootstrap",
            "the standing lease mode has no room for the embeddings server; stopped it again",
            mode=lease_mode,
        )

    # Before the wait, not after: a first install that is still loading weights
    # must not be the reason the lease script is missing when foundry asks.
    lease_script = install_lease_script(data_dir)
    install_broker_units(data_dir, router_port, lease_script)
    # Before the leased early-return: the proxy is the only thing listening on
    # LLAMA_PORT, so a deploy during a window must not leave it on old code.
    install_policy_unit(data_dir, port, router_port, lease_script)
    save_household_profile(data_dir)

    if leased:
        print("✅ llama-server is under a GPU lease; nothing was changed.")
        print(f"   GPU lease: python3 {lease_script} release")
        return 0

    household_preset = env_profile()["alias"]
    jlog(
        "info",
        "llama:bootstrap",
        "waiting for llama-server",
        url=llama_url,
        preset=household_preset,
        deadline_sec=min(stall_sec, 900),
    )
    # The router answers before any model is loaded, so the household preset is
    # asked for a token: that is both the readiness signal and the warm-up the
    # first resident turn would otherwise pay for (#1416).
    if not warm_preset(llama_url, household_preset, min(stall_sec, 900)):
        jlog(
            "warn",
            "llama:bootstrap",
            "llama-server did not serve the household preset. Check `journalctl --user -u llama.service` — a missing or truncated GGUF, or a presets file it could not parse, is the usual cause.",
            url=llama_url,
            preset=household_preset,
        )
        return 0

    if speculative_active(llama_url, household_preset):
        jlog("info", "llama:bootstrap", "speculative decoding active (MTP drafter)")
    else:
        jlog(
            "warn",
            "llama:bootstrap",
            "llama-server is up but /slots reports no speculative decoding — the drafter is not in play and answers will take about twice as long. Check LLAMA_DRAFT_FILE.",
        )

    # Against the proxy, not the router: LLAMA_PORT is the door every consumer
    # uses, so a dead proxy has to read as a dead service.
    register_http_check(sb_api, sb_token, policy_url)

    if embed["port"]:
        embed_url = f"http://127.0.0.1:{embed['port']}"
        if wait_for_ready(embed_url, deadline_sec=180) and embed_reachable(
            embed["port"]
        ):
            register_http_check(
                sb_api, sb_token, embed_url, "llama-embed-api", "llama.cpp embeddings"
            )
            jlog(
                "info",
                "llama:embed",
                "embeddings server answering /v1/embeddings",
                url=embed_url,
                model=embed["alias"],
            )
        else:
            jlog(
                "warn",
                "llama:embed",
                "the embeddings server did not answer /v1/embeddings — the vault's semantic search is degraded to keyword hits. Check `journalctl --user -u llama-embed.service`.",
                url=embed_url,
            )

    print(f"✅ llama-server is running on 127.0.0.1:{router_port} in router mode.")
    print(f"   Models in {models_dir} (from https://huggingface.co/{repo}).")
    print(f"   Presets: {', '.join(preset_profiles())} (pick one per request).")
    print(
        f"   Clients use :{port} — the mode policy proxy "
        f"({POLICY_UNIT}.service), which refuses a preset the standing "
        "lease mode does not allow. The Solaris Engine reaches it via "
        "LLAMA_SERVER_URL."
    )
    if embed["port"]:
        print(
            f"   Embeddings on 127.0.0.1:{embed['port']} ({embed['alias']}), "
            "reached via LLAMA_EMBED_URL."
        )
    if lease_script:
        print(f"   GPU lease: python3 {lease_script} acquire <name> | release")
        print(
            f"   Modes: python3 {lease_script} acquire <name> "
            "--model erweitert --duration 4h"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())

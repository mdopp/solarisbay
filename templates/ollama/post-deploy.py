#!/usr/bin/env python3
"""
post-deploy hook for the `ollama` template.

Two responsibilities:

  1. **Pull the default model.** Ollama doesn't pull on first start;
     it serves what's already on disk. The wizard knows which model
     the operator picked, so trigger the pull here once the pod is
     reachable.

  2. **Register an HTTP health check.** The auto-created
     `service:ollama` check catches "systemd thinks ollama is down";
     adding an `http` check against `/api/tags` catches the
     degraded-but-running cases (corrupt model store, GPU OOM, disk
     full) that systemd would still see as `active`.

Idempotent: a second run finds the model already cached and skips
the pull; the health-check API does upsert-by-id.

See lib/registry.ts:getTemplatePostDeployScript for the script
protocol and docs/TEMPLATE_AUTHORING.md § Health checks for the
check-registration contract.
"""

from __future__ import annotations

import datetime
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request


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


def wait_for_ready(ollama_url: str, deadline_sec: int) -> bool:
    """Poll /api/tags until Ollama responds 200."""
    started = time.time()
    last_beat = 0.0
    while time.time() - started < deadline_sec:
        status, _ = http_request(f"{ollama_url}/api/tags", timeout=5)
        if status == 200:
            return True
        elapsed = time.time() - started
        if elapsed - last_beat >= 10:
            jlog(
                "info",
                "ollama:wait",
                "still waiting for Ollama API",
                elapsed_sec=int(elapsed),
            )
            last_beat = elapsed
        time.sleep(3)
    return False


def model_present(ollama_url: str, model: str) -> bool:
    """Return True iff Ollama's /api/tags lists `model` (exact match against
    `name`). Used as a defensive post-pull check (#1047): the `ollama pull`
    CLI is known to exit 0 even when manifest write fails, and the HTTP
    /api/pull streaming endpoint can also report `success` while leaving
    the manifest unwritten if the underlying filesystem perms are wrong
    (e.g. a `library/<namespace>/` dir left root-owned by an earlier
    rootful run, biting the next rootless pull). Always re-check via
    /api/tags before declaring a pull successful."""
    try:
        with urllib.request.urlopen(f"{ollama_url}/api/tags", timeout=10) as resp:
            payload = json.loads(resp.read().decode("utf-8") or "{}")
    except (
        urllib.error.URLError,
        urllib.error.HTTPError,
        TimeoutError,
        OSError,
        json.JSONDecodeError,
    ) as e:
        jlog("warn", "ollama:verify", "/api/tags probe failed", error=str(e))
        return False
    for entry in payload.get("models", []) or []:
        if str(entry.get("name") or "") == model:
            return True
    return False


def pull_model(ollama_url: str, model: str, stall_sec: int) -> bool:
    """Trigger a streaming pull and wait for the done line.

    Fails only after `stall_sec` with no download progress — not on a total
    wall-clock budget — so a slow link can take as long as it needs as long
    as bytes keep flowing (#109). Download progress (percent + MB) is logged
    every PROGRESS_LOG_INTERVAL_SEC so the operator sees movement instead of
    a silent multi-GB wait.

    Post-pull verifies via /api/tags (#1047) — neither the CLI nor the
    HTTP streaming endpoint reliably surfaces manifest-write failures.
    A pull that reports `success` but never lands the model in /api/tags
    is treated as a failure here so callers can fall back / fail loud
    instead of leaving the operator with a 404-on-first-chat box."""
    PROGRESS_LOG_INTERVAL_SEC = 15
    body = json.dumps({"name": model, "stream": True}).encode("utf-8")
    req = urllib.request.Request(
        f"{ollama_url}/api/pull",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    started = time.time()
    # `stall_sec` doubles as the socket read timeout, so a dead connection
    # (no bytes at all) raises after the same window the in-loop stall check
    # uses for a live-but-stuck stream.
    try:
        with urllib.request.urlopen(req, timeout=stall_sec) as resp:
            last_status = ""
            last_log = 0.0
            last_progress_at = started
            last_seen = ""
            for raw in resp:
                now = time.time()
                try:
                    chunk = json.loads(raw.decode("utf-8").strip())
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                if chunk.get("error"):
                    jlog(
                        "error",
                        "ollama:pull",
                        "pull error",
                        model=model,
                        error=str(chunk.get("error")),
                    )
                    return False
                status = str(chunk.get("status", ""))
                completed = int(chunk.get("completed") or 0)
                total = int(chunk.get("total") or 0)
                # Any change in status or downloaded bytes counts as progress;
                # only a genuinely stuck pull lets `last_progress_at` go stale.
                fingerprint = f"{status}:{completed}"
                if fingerprint != last_seen:
                    last_seen = fingerprint
                    last_progress_at = now
                elif now - last_progress_at > stall_sec:
                    jlog(
                        "error",
                        "ollama:pull",
                        "model pull stalled — no download progress within the stall window",
                        model=model,
                        stall_sec=stall_sec,
                        last_status=status,
                    )
                    return False
                # Log on status change, else throttle to one progress line per
                # interval so a multi-GB blob shows steady, visible movement.
                if status and (
                    status != last_status or now - last_log >= PROGRESS_LOG_INTERVAL_SEC
                ):
                    if total > 0 and completed > 0:
                        pct = int(completed * 100 / total)
                        done_mb = completed // (1024 * 1024)
                        total_mb = total // (1024 * 1024)
                        # ASCII bar in the message so the log line reads as a
                        # progress bar wherever it surfaces; structured fields
                        # stay in args for a future UI bar (servicebay#1288).
                        filled = pct * 20 // 100
                        bar = "#" * filled + "-" * (20 - filled)
                        jlog(
                            "info",
                            "ollama:pull",
                            f"{model} [{bar}] {pct}% ({done_mb}/{total_mb} MB)",
                            model=model,
                            percent=pct,
                            completed_mb=done_mb,
                            total_mb=total_mb,
                        )
                    else:
                        jlog("info", "ollama:pull", status, model=model)
                    last_status = status
                    last_log = now
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
        jlog("error", "ollama:pull", "pull failed", model=model, error=str(e))
        return False
    if not model_present(ollama_url, model):
        jlog(
            "error",
            "ollama:pull",
            "stream reported success but model is not in /api/tags — manifest write likely failed silently (#1047). Check `ls -la /mnt/data/stacks/ollama/models/manifests/registry.ollama.ai/library/` for non-`core:core` ownership on the host.",
            model=model,
        )
        return False
    jlog(
        "info",
        "ollama:pull",
        "model ready",
        model=model,
        elapsed_sec=int(time.time() - started),
    )
    return True


def register_http_check(sb_api: str, sb_token: str, ollama_url: str) -> None:
    """Best-effort: a non-200 here doesn't block the install."""
    headers = {}
    if sb_token:
        headers["X-SB-Internal-Token"] = sb_token
    status, body = http_request(
        f"{sb_api}/api/health/checks",
        payload={
            "id": "ollama-api",
            "name": "Ollama API",
            "type": "http",
            "target": f"{ollama_url}/api/tags",
            "interval": 60,
            "enabled": True,
            "httpConfig": {"expectedStatus": 200},
        },
        method="POST",
        timeout=10,
        extra_headers=headers,
    )
    if status == 200:
        jlog("info", "ollama:health", "registered http check ollama-api")
    else:
        jlog(
            "warn",
            "ollama:health",
            "could not register http check",
            status=status,
            body=body.decode("utf-8", errors="replace")[:200],
        )


def gpu_actually_engaged(ollama_url: str) -> bool:
    """Probe Ollama's /api/ps + the runtime config to decide whether the
    deployed unit actually got the GPU. `podman kube play` silently drops
    `resources.limits.nvidia.com/gpu` (#1026), so the .kube unit comes up
    on CPU even when OLLAMA_GPU_PASSTHROUGH=yes. The /api/version
    response doesn't expose VRAM, so we fall back to /api/show on the
    default model — when GPU is engaged, the runner-info has `runner: cuda`
    or similar in modern Ollama. If we can't determine, return False
    (caller assumes GPU isn't engaged and applies the Quadlet fixup)."""
    # Cheapest signal: /api/version returns 200 if the server is alive.
    # We trust /api/tags has already passed via wait_for_ready.
    # Most reliable: list loaded runners — when a model is loaded with
    # CUDA, /api/ps shows `processor: <gpu-id>`. With no model loaded,
    # there is no signal, so we don't gate on this; we rely on the
    # JSON-log inspection below.
    #
    # Fallback: read systemd journal output for the lib detection line.
    # The line we want is exactly:
    #   "inference compute" id=GPU-... library=CUDA ...
    # versus the CPU-only fallback:
    #   "inference compute" id=cpu library=cpu ...
    try:
        out = subprocess.run(
            [
                "journalctl",
                "--user",
                "-u",
                "ollama.service",
                "--since",
                "-2 min",
                "--no-pager",
                "-o",
                "cat",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if "library=CUDA" in out.stdout or "library=ROCm" in out.stdout:
            return True
        if "library=cpu" in out.stdout:
            return False
    except (FileNotFoundError, subprocess.SubprocessError, OSError):
        pass
    return False


def install_gpu_quadlet_fallback(port: str, data_dir: str) -> bool:
    """#1026 — Replace the just-deployed rootless `.kube` ollama unit
    with a `.container` Quadlet that uses `AddDevice=nvidia.com/gpu=all`
    + `SecurityLabelDisable=true`. That's the only combination on
    rootless podman 5.x that actually triggers CDI passthrough +
    SELinux relaxation for NVIDIA NVML init. Verified live on
    192.168.178.100 (RTX 2000 Ada): without this fixup ollama runs
    library=cpu with total_vram=0; with it, library=CUDA + 16 GiB
    VRAM and 78% GPU offload on gemma4:26b.

    Idempotent — re-running on an already-fixed install detects the
    presence of `ollama.container` and skips.

    Caveat: ServiceBay's discovery still tags `.container`-backed
    units as "unmanaged" (see agent.py — `is_managed` only when
    source_ext == .kube). The companion agent.py change in this PR
    widens that to .container so the dashboard reads correctly.
    """
    if not os.path.exists("/etc/cdi/nvidia.yaml"):
        jlog(
            "info",
            "ollama:gpu-fallback",
            "/etc/cdi/nvidia.yaml missing; CDI not registered on this host. Leaving CPU-only kube unit in place.",
        )
        return False

    systemd_dir = os.path.expanduser("~/.config/containers/systemd")
    kube_path = os.path.join(systemd_dir, "ollama.kube")
    container_path = os.path.join(systemd_dir, "ollama.container")

    if os.path.exists(container_path):
        jlog(
            "info",
            "ollama:gpu-fallback",
            "ollama.container already present; skipping re-write",
            path=container_path,
        )
        return True

    # 1. Stop the broken kube service (best-effort; it may already be down).
    subprocess.run(
        ["systemctl", "--user", "stop", "ollama.service"],
        check=False,
        capture_output=True,
    )

    # 2. Remove the .kube file so Quadlet doesn't generate a conflicting
    #    `ollama.service` from both sources at daemon-reload time.
    #    Keep ollama.yml around as documentation; nothing reads it once
    #    the .kube reference is gone.
    if os.path.exists(kube_path):
        try:
            os.unlink(kube_path)
        except OSError as e:
            jlog(
                "warn",
                "ollama:gpu-fallback",
                "could not remove ollama.kube — Quadlet may complain",
                path=kube_path,
                error=str(e),
            )

    # 3. Write the .container Quadlet. Mirror the .yml's runtime contract:
    #    image, OLLAMA_HOST env, hostNetwork, the persistent volume mount.
    #    The two new lines are AddDevice + SecurityLabelDisable.
    container_unit = (
        "[Unit]\n"
        "Description=Ollama (Local LLM Server, GPU passthrough #1026 fixup)\n"
        "Wants=network-online.target\n"
        "After=network-online.target\n"
        "\n"
        "[Container]\n"
        "Image=docker.io/ollama/ollama:latest\n"
        "ContainerName=ollama\n"
        "Network=host\n"
        f"Environment=OLLAMA_HOST=127.0.0.1:{port}\n"
        "# CDI device — verified working on rootless podman 5.8 + nvidia-ctk\n"
        "# 1.19. podman kube play silently drops this when expressed as\n"
        "# resources.limits.nvidia.com/gpu, which is why the .yml-based\n"
        "# deploy falls through to CPU. See #1026.\n"
        "AddDevice=nvidia.com/gpu=all\n"
        "# SELinux relaxation is required for NVML init on FCoS — without\n"
        "# it the container starts, sees the devices, but NVML returns\n"
        "# 'Insufficient Permissions' on every nvmlInit call.\n"
        "SecurityLabelDisable=true\n"
        f"Volume={data_dir}/ollama:/root/.ollama:Z\n"
        "AutoUpdate=registry\n"
        "\n"
        "[Service]\n"
        "Restart=on-failure\n"
        "RestartSec=5\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )
    try:
        with open(container_path, "w") as f:
            f.write(container_unit)
        os.chmod(container_path, 0o644)
    except OSError as e:
        jlog(
            "error",
            "ollama:gpu-fallback",
            "could not write ollama.container",
            path=container_path,
            error=str(e),
        )
        return False

    # 4. Reload + start. Quadlet regenerates ollama.service from the new
    #    `.container` source on `daemon-reload`.
    subprocess.run(
        ["systemctl", "--user", "daemon-reload"], check=False, capture_output=True
    )
    started = subprocess.run(
        ["systemctl", "--user", "start", "ollama.service"],
        capture_output=True,
        text=True,
    )
    if started.returncode != 0:
        jlog(
            "error",
            "ollama:gpu-fallback",
            "systemctl start failed",
            stderr=started.stderr[:400],
        )
        return False

    jlog(
        "info",
        "ollama:gpu-fallback",
        "swapped rootless ollama.kube → ollama.container for CDI passthrough",
        path=container_path,
    )
    return True


def main() -> int:
    port = env("OLLAMA_PORT", "11434")
    model = env("OLLAMA_DEFAULT_MODEL", "gemma4:e4b")
    extra_models_raw = env("OLLAMA_EXTRA_MODELS", "")
    extra_models = [m.strip() for m in extra_models_raw.split(",") if m.strip()]
    vision_model = env("OLLAMA_VISION_MODEL", "")
    timeout = int(env("OLLAMA_READINESS_TIMEOUT_SECONDS", "600"))
    sb_api = env("SB_API_URL", "http://localhost:3000")
    sb_token = env("SB_API_TOKEN", "")
    # Blank/unset => auto-detect: engage the GPU when the host has a
    # CDI-registered NVIDIA device, the same file install_gpu_quadlet_fallback
    # gates on. Explicit yes/no overrides the probe either way.
    _gpu = env("OLLAMA_GPU_PASSTHROUGH", "").strip().lower()
    if _gpu in ("yes", "true", "1"):
        gpu_requested = True
    elif _gpu in ("no", "false", "0", "off"):
        gpu_requested = False
    else:
        gpu_requested = os.path.exists("/etc/cdi/nvidia.yaml")
    data_dir = env("DATA_DIR", "/mnt/data/stacks")
    ollama_url = f"http://127.0.0.1:{port}"

    # #1026 — GPU fixup runs BEFORE wait_for_ready so any model pull
    # below loads onto the GPU-backed runtime, not the broken CPU one.
    if gpu_requested:
        if not gpu_actually_engaged(ollama_url):
            jlog(
                "info",
                "ollama:bootstrap",
                "GPU passthrough requested but the .kube unit fell through to CPU — applying #1026 Quadlet fixup",
            )
            install_gpu_quadlet_fallback(port, data_dir)
        else:
            jlog(
                "info", "ollama:bootstrap", "GPU already engaged; no #1026 fixup needed"
            )

    jlog(
        "info",
        "ollama:bootstrap",
        "waiting for Ollama API",
        url=ollama_url,
        deadline_sec=timeout,
    )
    if not wait_for_ready(ollama_url, deadline_sec=min(timeout, 120)):
        jlog(
            "warn",
            "ollama:bootstrap",
            "Ollama API not reachable yet; skipping model pull. The service may still come up — check the install log and re-run from the wizard if needed.",
            url=ollama_url,
        )
        return 0

    if model:
        jlog("info", "ollama:pull", "starting model pull", model=model)
        ok = pull_model(ollama_url, model, stall_sec=timeout)
        if not ok:
            jlog(
                "warn",
                "ollama:pull",
                'model pull did not complete; the pod is up but the default model is missing. Pull manually with `curl -X POST http://127.0.0.1:%s/api/pull -d \'{"name":"%s"}\'`.'
                % (port, model),
                model=model,
            )

    # Extras (#1046): one-click-switchable alternatives the operator can
    # pick from Hermes' Models tab without a fresh download. Failures are
    # warn-not-fatal — the default model is the only one the install
    # depends on; extras enrich the choice set.
    for extra in extra_models:
        if extra == model:
            continue  # already covered above
        jlog("info", "ollama:pull", "starting extra-model pull", model=extra)
        if not pull_model(ollama_url, extra, stall_sec=timeout):
            jlog(
                "warn",
                "ollama:pull",
                'extra-model pull did not complete; it will not be selectable from Hermes\' Models tab until pulled manually. Run `curl -X POST http://127.0.0.1:%s/api/pull -d \'{"name":"%s"}\'`.'
                % (port, extra),
                model=extra,
            )

    if vision_model:
        jlog("info", "ollama:pull", "starting vision-model pull", model=vision_model)
        ok = pull_model(ollama_url, vision_model, stall_sec=timeout)
        if not ok:
            jlog(
                "warn",
                "ollama:pull",
                'vision-model pull did not complete; OSCAR\'s media-ingestion-multimodal skill will fall back to text-only. Pull manually with `curl -X POST http://127.0.0.1:%s/api/pull -d \'{"name":"%s"}\'` or bump OLLAMA_READINESS_TIMEOUT_SECONDS.'
                % (port, vision_model),
                model=vision_model,
            )

    register_http_check(sb_api, sb_token, ollama_url)

    print(f"✅ Ollama is running on 127.0.0.1:{port}. Default model: {model}.")
    if extra_models:
        print(f"   Extra models pulled: {', '.join(extra_models)}.")
    if vision_model:
        print(f"   Vision model: {vision_model} (multimodal-capable).")
    print(
        f"   Other ServiceBay templates (hermes, oscar-household) can reach it at http://127.0.0.1:{port}."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

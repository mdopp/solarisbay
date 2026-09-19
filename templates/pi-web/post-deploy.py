#!/usr/bin/env python3
"""
post-deploy hook for the `pi-web` template.

Three responsibilities:

  1. **Point the Pi agent at the model.** PI WEB has no LLM configuration of
     its own — the model runtime is the Pi Coding Agent's, and a self-hosted
     OpenAI-compatible endpoint is declared in the agent directory's
     `models.json`. This writes that file into the volume the pod mounts.

     Since #1435 the `baseUrl` is the pod's own gate, `http://127.0.0.1:11437/v1`
     — the containers of this pod share one network namespace, so that is the
     `model-gate` container beside the sessions, and it forwards to the policy
     proxy on `LLAMA_PORT` as `host.containers.internal`. Not `127.0.0.1` for
     *that* hop (this pod has its own netns), not the LAN address (rootless
     podman refuses it), and never the `llama.<domain>` route, which is
     Authelia-gated and exists for a human with a browser.

     It declares the CONNECTION only. The model list itself comes from the Pi
     extension `pi-web/extensions/solaris-llama.js`, which registers the same
     provider in native form with a real `fetchModels` so Pi's own hourly
     catalog refresh keeps the picker current (#1435). A `models` array here
     would override that fetched list entry for entry and be the stale source
     of truth again — which is exactly how `gemma-4-12b` stayed unpickable for
     six days after #1431 started serving it.

  3. **Bridge the gate's lease wishes to the Engine (#1435).** Picking a model
     in PI WEB now takes the mode that permits it (operator, 2026-09-19). The
     pod cannot make that call itself — `/api/model-lease` is loopback-only and
     unauthenticated, and this pod has its own netns — so the gate writes a wish
     onto the shared volume and `pi-web-lease-broker.path` starts a oneshot
     service that makes the call, holder `pi-web`, and writes the answer back.
     Same pattern and the same reason as the Engine's own
     `solaris-gpu-lease-broker` (#1333). Demand-driven, never long-running:
     that is the difference from the unit #1392 retired.

  2. **Retire the host-side lease unit (#1392).** Until now PI WEB took the
     coding lease by simply being started: `pi-web-model-lease.service` was
     `BindsTo=pi-web.service`, so a start — including ServiceBay's own start on
     every deploy, and the box's after every reboot — loaded Qwen, moved voice
     onto the CPU and left the household assistant slow for up to four hours
     that nobody had asked for. The counterweight was to keep PI WEB switched
     off (#1373: strip the platform's `[Install]`, restore the pre-deploy run
     state), which left `pi.<domain>` dead until somebody started the service
     by hand.

     Since #1374/#1381 the lease has an operator-facing route of its own — the
     model tile in Solaris — so PI WEB no longer needs one. It runs around the
     clock like any other service (the platform's `[Install]` stays), and this
     script stops, disables and removes the lease unit and its script copy. A
     window still filed under holder `pi-web` from before the upgrade is closed
     once, here, so the box does not sit on Qwen until the TTL runs out.

Idempotent: identical `models.json` is left alone, and a lease unit that is
already gone (and a lease that is not ours) is a no-op.

See lib/registry.ts:getTemplatePostDeployScript for the script protocol.
ServiceBay Mustache-renders this file before executing it, so it carries no
double-brace tags at all — every value comes from `env()`
(templates/tests/test_post_deploy_mustache.py).
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

# The Solaris Engine names its leases in one word each; `coding` is the one
# that loads Qwen. `pi-web` is the holder this service used to file its windows
# under (#1347) — kept because the one-time cleanup below has to recognise them.
LEASE_HOLDER = "pi-web"

LEASE_UNIT = "pi-web-model-lease"
LEASE_SCRIPT = "pi-web-lease.py"
POD_UNIT = "pi-web.service"
SYSTEMD_USER_DIR = "~/.config/systemd/user"

# The bridge across the pod's network namespace (#1435). PI WEB may take the
# mode itself now (operator, 2026-09-19), but the pod cannot: `/api/model-lease`
# is loopback-only with no token — reachability IS the authorisation — and this
# pod has its own netns (ADR 0007). So `pi-web-model-gate` writes a wish onto
# the volume and the units below turn it into the very lease call a host script
# would make, holder `pi-web`.
#
# This is NOT the unit #1392 removed. That one was `BindsTo=pi-web.service`:
# PI WEB runs around the clock, so it took the card on every start and boot for
# hours nobody had asked for. This one is a `.path` watcher with a oneshot
# service — it exists only while a wish is pending and ends with it.
BROKER_UNIT = "pi-web-lease-broker"
BROKER_SCRIPT = "pi-lease-broker.py"
LEASE_DIR_NAME = "model-lease"
LEASE_REQUEST_FILE = "request.json"
LEASE_STATUS_FILE = "status.json"

# How long the broker waits for the box to finish a mode switch before it says
# so. The switch was box-measured at ~56 s including the environment change;
# a first `foundry` window on a fresh box downloads 8 GB first, which is what
# the unit's own `TimeoutStartSec` covers.
BROKER_DEADLINE_SEC = 300

# How often the waiting session is told to look at the status file again. It
# travels in the `preparing` record as `retry_after`, so the pod reads a cadence
# off the answer instead of carrying a second copy of this number.
GATE_POLL_SEC = 5
QUADLET_DIR = "~/.config/containers/systemd"
KUBE_UNIT = "pi-web.kube"
BOOT_INSTALL = "[Install]\nWantedBy=default.target\n"

# llama-server ships no authentication, so there is no key to hold — but Pi
# hides a model whose provider has no auth configured at all, so the provider
# carries a placeholder, exactly as upstream's own Ollama example does.
LLAMA_PLACEHOLDER_KEY = "llama"

PROVIDER_ID = "solaris-llama"

# Where `models.json` sends the sessions: the pod's own model gate, not
# `LLAMA_PORT` on the host. Every container of this pod shares one network
# namespace, so `127.0.0.1` here is the gate container beside them — and routing
# the sessions through it is what lets a refused preset take the mode it needs
# instead of coming back as an error. A constant rather than a variable for the
# same reason as the presets above: the port the gate binds and the port this
# file writes are one number or the sessions have no model at all.
MODEL_GATE_PORT = "11437"


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
) -> tuple[int, dict]:
    """`(status, decoded body)`. Status 0 means the engine did not answer."""
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method=method
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, decode_body(resp.read())
    except urllib.error.HTTPError as e:
        try:
            body = e.read()
        except Exception:  # pylint: disable=broad-except
            body = b""
        return e.code, decode_body(body)
    except (urllib.error.URLError, TimeoutError, OSError):
        return 0, {}


def decode_body(raw: bytes) -> dict:
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return {}
    return body if isinstance(body, dict) else {}


# ── pure decision logic (unit-tested in templates/tests) ─────────────────────


def lease_url(chat_port: str) -> str:
    return f"http://127.0.0.1:{chat_port}/api/model-lease"


def is_own_stale_window(status: int, body: dict) -> bool:
    """True when the window standing right now is this service's own leftover.

    The retired lease unit filed its windows under holder `pi-web` (#1347), and
    one of them can still be open when this upgrade lands — the unit is removed
    below, so nothing would ever give that card back and the box would sit on
    Qwen until the TTL ran out. The holder is what makes "ours" distinguishable
    from "somebody else's", including the model tile's (#1374), whose window
    must be left alone. Nothing open, or somebody else's: no-op.
    """
    return (
        status == 200
        and body.get("state") in ("preparing", "ready")
        and body.get("holder") == LEASE_HOLDER
    )


def acquire_outcome(status: int, body: dict) -> str:
    """What a `POST /api/model-lease` answer means, by its documented status.

    200 the window stands, 202 the box is switching, 409 somebody else holds it,
    anything else is a refusal or a silence. `state` is preferred over the code
    where the body carries one, because that is the field the contract
    (mdopp/foundry-chronicle#321) calls authoritative.
    """
    if status == 200:
        return str(body.get("state") or "ready")
    if status == 202:
        return "preparing"
    if status == 409:
        return "held"
    return "error"


def poll_cadence(body: dict) -> float:
    """`retry_after` is how often to ask again, not how long the switch takes —
    so it is the sleep between two `GET`s and never a one-shot wait."""
    value = body.get("retry_after")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return 5.0
    return float(value)


def lease_dir(data_dir: str) -> str:
    """The exchange, on the volume the pod mounts at /data (template.yml)."""
    return os.path.join(data_dir, "pi-web", "data", LEASE_DIR_NAME)


def request_path(data_dir: str) -> str:
    return os.path.join(lease_dir(data_dir), LEASE_REQUEST_FILE)


def status_path(data_dir: str) -> str:
    return os.path.join(lease_dir(data_dir), LEASE_STATUS_FILE)


def render_broker_units(data_dir: str, chat_port: str, script: str) -> tuple[str, str]:
    """The `.path`/`.service` pair, pure so the test can read them.

    Demand-driven on purpose: `Type=oneshot`, started by the write itself and
    gone again afterwards. That is the difference from the unit #1392 retired,
    which was bound to the pod and therefore held the card for as long as PI WEB
    ran — which is always.
    """
    path_unit = (
        "[Unit]\n"
        "Description=Watch for a PI WEB model-lease wish (#1435)\n"
        "\n"
        "[Path]\n"
        f"PathChanged={request_path(data_dir)}\n"
        f"Unit={BROKER_UNIT}.service\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )
    service_unit = (
        "[Unit]\n"
        "Description=Take or give back the GPU mode PI WEB asked for (#1435)\n"
        "\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"Environment=DATA_DIR={data_dir}\n"
        f"Environment=CHAT_PORT={chat_port}\n"
        # A first `foundry` window downloads 8 GB before it switches anything.
        "TimeoutStartSec=3600\n"
        f"ExecStart={sys.executable} {script} broker\n"
    )
    return path_unit, service_unit


def add_boot_install(kube_text: str) -> str:
    """The `.kube` unit with an `[Install] WantedBy=default.target` section.

    Unchanged when the section is already there — that is the ordinary case, so
    the file is not rewritten and the generator not reloaded on every deploy.
    The one box this actually edits is the one #1373 stripped.
    """
    if any(line.strip() == "[Install]" for line in kube_text.splitlines()):
        return kube_text
    separator = "" if kube_text.endswith("\n\n") else "\n"
    return kube_text + separator + BOOT_INSTALL


def models_document(gate_port: str) -> dict:
    """The Pi agent's `models.json`: the provider CONNECTION and nothing else.

    It deliberately lists no models (#1435). The model list is now produced by
    the Pi extension `solaris-llama.js`, which registers this same provider in
    native form with a real `fetchModels` and lets Pi's own hourly background
    refresh keep it current. A `models` array here would not merely duplicate
    that list — `applyModelsJson` in `@earendil-works/pi-coding-agent` upserts a
    models.json entry OVER the fetched one of the same id, so the hand-written
    copy would win and this file would be the stale source of truth all over
    again. That is the bug this unit removes: the file dated 13.09. still named
    three presets after #1431 had made four visible.

    What stays is what Pi needs before any extension has run: where the provider
    is, which dialect it speaks, and a key so it is not hidden. If the extension
    fails to load, PI WEB shows this provider with no models — visible, and the
    sessiond log says which extension failed — rather than quietly serving an
    old list.

    The `baseUrl` is the pod's own gate, which forwards to the policy proxy on
    `LLAMA_PORT` and, when the standing mode refuses the wanted preset, asks the
    box for the mode that permits it. Not `127.0.0.1` for *that* hop (this pod
    has its own netns), not the LAN address (rootless podman refuses it), and
    never the `llama.<domain>` route, which is Authelia-gated and exists for a
    human with a browser.
    """
    return {
        "providers": {
            PROVIDER_ID: {
                "baseUrl": f"http://127.0.0.1:{gate_port}/v1",
                "api": "openai-completions",
                "apiKey": LLAMA_PLACEHOLDER_KEY,
                # llama-server takes neither the `developer` role nor
                # `reasoning_effort` — asking for either turns every request
                # into a 400. `chat-template` is the thinking dialect
                # llama.cpp speaks: `chat_template_kwargs.enable_thinking`.
                "compat": {
                    "supportsDeveloperRole": False,
                    "supportsReasoningEffort": False,
                    "thinkingFormat": "chat-template",
                },
            }
        }
    }


# ── the box side ────────────────────────────────────────────────────────────


def agent_dir(data_dir: str) -> str:
    """The Pi agent directory as the containers see it at /data/pi-agent."""
    return os.path.join(data_dir, "pi-web", "data", "pi-agent")


def kube_unit_path() -> str:
    return os.path.join(os.path.expanduser(QUADLET_DIR), KUBE_UNIT)


def write_models_json(data_dir: str, gate_port: str) -> bool:
    path = os.path.join(agent_dir(data_dir), "models.json")
    text = json.dumps(models_document(gate_port), indent=2) + "\n"
    try:
        if os.path.exists(path) and open(path, encoding="utf-8").read() == text:
            jlog("info", "pi-web:models", "models.json already current", path=path)
            return True
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
    except OSError as e:
        jlog(
            "error",
            "pi-web:models",
            "could not write models.json; PI WEB will start with no model to pick",
            path=path,
            error=str(e),
        )
        return False
    jlog(
        "info",
        "pi-web:models",
        "models.json written",
        path=path,
        provider=PROVIDER_ID,
        models="from the model gate, via the solaris-llama extension",
    )
    return True


# ── the host half of the bridge (#1435) ─────────────────────────────────────


def read_lease_request(data_dir: str) -> dict:
    try:
        with open(request_path(data_dir), encoding="utf-8") as f:
            record = json.load(f)
    except (OSError, ValueError):
        return {}
    return record if isinstance(record, dict) else {}


def write_lease_status(data_dir: str, record: dict) -> None:
    path = status_path(data_dir)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(record, f)
        os.chmod(path, 0o644)
    except OSError as e:
        jlog(
            "error",
            "pi-web:broker",
            "could not write the lease status; the session will wait for nothing",
            path=path,
            error=str(e),
        )


def broker_acquire(url: str, correlation: str, mode: str, ttl: int) -> dict:
    """Take `mode` for holder `pi-web` and wait for it to stand.

    Never a takeover: a 409 is reported with the holder and the end time the
    lease API returns, which is what the session then shows.
    """
    status, body = http_request(
        url, {"model": mode, "ttl_s": ttl, "holder": LEASE_HOLDER}, "POST"
    )
    outcome = acquire_outcome(status, body)
    if outcome == "held":
        return {
            "id": correlation,
            "state": "held",
            "mode": mode,
            "holder": str(body.get("holder") or ""),
            "expires_at": body.get("expires_at"),
        }
    if outcome == "error":
        return {
            "id": correlation,
            "state": "error",
            "mode": mode,
            "message": f"Die Lease-Schnittstelle antwortete {status or 'gar nicht'}.",
        }
    deadline = time.time() + BROKER_DEADLINE_SEC
    while outcome != "ready" and time.time() < deadline:
        time.sleep(poll_cadence(body))
        status, body = http_request(url, None, "GET")
        outcome = str(body.get("state") or "") if status == 200 else "error"
        if outcome == "none":
            return {
                "id": correlation,
                "state": "error",
                "mode": mode,
                "message": "Das Fenster ist wieder verschwunden, bevor es stand.",
            }
    if outcome != "ready":
        return {
            "id": correlation,
            "state": "error",
            "mode": mode,
            "message": "Der Umbau lief in eine Zeitgrenze.",
        }
    return {
        "id": correlation,
        "state": "ready",
        "mode": mode,
        "holder": LEASE_HOLDER,
        "alias": str(body.get("alias") or ""),
        "expires_at": body.get("expires_at"),
        "renew_after": body.get("renew_after"),
    }


def broker_release(url: str, correlation: str, mode: str) -> dict:
    """Give the window back, once. A `releasing` state is waited out rather than
    answered with a second DELETE — the host finishes it whether anyone polls or
    not (#1364)."""
    status, body = http_request(url, {"holder": LEASE_HOLDER}, "DELETE")
    if status == 409:
        return {
            "id": correlation,
            "state": "held",
            "mode": mode,
            "holder": str(body.get("holder") or ""),
            "expires_at": body.get("expires_at"),
        }
    deadline = time.time() + BROKER_DEADLINE_SEC
    while time.time() < deadline:
        status, body = http_request(url, None, "GET")
        if status == 200 and body.get("state") == "none":
            return {"id": correlation, "state": "released", "mode": mode}
        time.sleep(poll_cadence(body))
    return {
        "id": correlation,
        "state": "error",
        "mode": mode,
        "message": "Die Rückgabe läuft noch; die Box beendet sie von selbst.",
    }


def broker_run(data_dir: str, chat_port: str) -> int:
    """What `pi-web-lease-broker.service` runs: one wish, one answer.

    Idempotent against a path unit that fires twice — an id already answered is
    left alone rather than re-run, which for `acquire` would re-arm a window and
    for `release` would be the second DELETE the contract forbids.
    """
    request = read_lease_request(data_dir)
    correlation = str(request.get("id") or "")
    if not correlation:
        return 0
    answered = read_lease_status_id(data_dir)
    if answered == correlation:
        return 0
    mode = str(request.get("mode") or "")
    op = str(request.get("op") or "")
    url = lease_url(chat_port)
    write_lease_status(
        data_dir,
        {
            "id": correlation,
            "state": "preparing",
            "mode": mode,
            "retry_after": GATE_POLL_SEC,
        },
    )
    if op == "release":
        record = broker_release(url, correlation, mode)
    elif op == "acquire" and mode:
        record = broker_acquire(
            url, correlation, mode, int(request.get("ttl_s") or 900)
        )
    else:
        record = {
            "id": correlation,
            "state": "error",
            "mode": mode,
            "message": "Unbekannter Wunsch.",
        }
    write_lease_status(data_dir, record)
    jlog(
        "info",
        "pi-web:broker",
        "lease wish handled",
        op=op,
        mode=mode,
        state=record.get("state"),
    )
    return 0


def read_lease_status_id(data_dir: str) -> str:
    try:
        with open(status_path(data_dir), encoding="utf-8") as f:
            record = json.load(f)
    except (OSError, ValueError):
        return ""
    if not isinstance(record, dict) or record.get("state") == "preparing":
        return ""
    return str(record.get("id") or "")


def install_broker_script(data_dir: str) -> str:
    """Copy this script to a durable path the unit can execute — the same
    self-copy the llama lease broker uses, so the request format and the code
    that reads it are one file."""
    dst = os.path.join(data_dir, "pi-web", BROKER_SCRIPT)
    try:
        with open(os.path.realpath(__file__), encoding="utf-8") as f:
            source = f.read()
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        with open(dst, "w", encoding="utf-8") as f:
            f.write(source)
        os.chmod(dst, 0o755)
    except OSError as e:
        jlog(
            "warn",
            "pi-web:broker",
            "could not install the lease broker script",
            path=dst,
            error=str(e),
        )
        return ""
    return dst


def install_broker_units(data_dir: str, chat_port: str, script: str) -> None:
    """Write + enable the wish watcher. Idempotent: same text, same enable.

    The exchange directory is opened to everyone on purpose. The pod's
    containers run as the image's `USER node`, which is a different host UID
    than this script's, and the pod's own perms init already keeps /data
    `a+rwX` for exactly that reason (#1358/#1403). Only a correlation id, a mode
    and a deadline are ever written here — never a token, because the lease API
    has none.
    """
    if not script:
        return
    unit_dir = os.path.expanduser(SYSTEMD_USER_DIR)
    path_unit, service_unit = render_broker_units(data_dir, chat_port, script)
    try:
        os.makedirs(unit_dir, exist_ok=True)
        os.makedirs(lease_dir(data_dir), exist_ok=True)
        os.chmod(lease_dir(data_dir), 0o777)
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
            "pi-web:broker",
            "could not install the lease broker; PI WEB will not be able to switch the mode",
            path=unit_dir,
            error=str(e),
        )
        return
    subprocess.run(
        ["systemctl", "--user", "daemon-reload"], check=False, capture_output=True
    )
    subprocess.run(
        ["systemctl", "--user", "enable", "--now", f"{BROKER_UNIT}.path"],
        check=False,
        capture_output=True,
    )
    jlog("info", "pi-web:broker", "lease broker installed", unit=f"{BROKER_UNIT}.path")


# ── retiring the host-side lease unit (#1392) ───────────────────────────────


def retire_lease_unit(data_dir: str) -> None:
    """Stop, disable and remove `pi-web-model-lease.service` and its script.

    Removing the unit file is not enough on its own: it is `BindsTo=` and
    `WantedBy=pi-web.service`, so a copy left enabled would keep being started
    with PI WEB — which now runs around the clock — and take the coding lease
    on every boot. `disable` is what drops the `pi-web.service.wants` link.
    This runs before pi-web is started below, so the start never passes a unit
    that is still linked to it.
    """
    unit = f"{LEASE_UNIT}.service"
    for verb in ("stop", "disable"):
        subprocess.run(
            ["systemctl", "--user", verb, unit], check=False, capture_output=True
        )
    removed = []
    for path in (
        os.path.join(os.path.expanduser(SYSTEMD_USER_DIR), unit),
        os.path.join(data_dir, "pi-web", LEASE_SCRIPT),
    ):
        try:
            os.remove(path)
            removed.append(path)
        except OSError:
            pass
    subprocess.run(
        ["systemctl", "--user", "daemon-reload"], check=False, capture_output=True
    )
    jlog(
        "info",
        "pi-web:lease",
        "lease unit retired; the coding lease now comes from the Solaris model tile",
        unit=unit,
        removed=removed,
    )


def release_own_lease(chat_port: str) -> None:
    """Give back a window still filed under holder `pi-web`, once."""
    url = lease_url(chat_port)
    status, body = http_request(url, None, "GET")
    if not is_own_stale_window(status, body):
        jlog(
            "info",
            "pi-web:lease",
            "no coding lease of ours to give back",
            state=body.get("state", ""),
            holder=body.get("holder", ""),
        )
        return
    status, body = http_request(url, {"holder": LEASE_HOLDER}, "DELETE")
    jlog(
        "info",
        "pi-web:lease",
        "released the coding lease the retired unit had taken",
        status=status,
    )


def restore_boot_autostart() -> None:
    """Put the `[Install]` section back into the `.kube` unit and reload.

    ServiceBay renders `[Install] WantedBy=default.target` into every `.kube`
    it writes, so a fresh install already has it — but a box upgraded from
    #1373 carries a unit this template *stripped*, and ServiceBay only rewrites
    the file when the rendered spec changed. Adding it back here covers both.

    Not `systemctl enable`: a Quadlet-generated unit cannot be enabled by
    systemctl at all. The `[Install]` section is read by the generator, which
    creates the `default.target.wants` link itself on the reload below.
    """
    path = kube_unit_path()
    try:
        with open(path, encoding="utf-8") as f:
            current = f.read()
    except OSError as e:
        jlog(
            "warn",
            "pi-web:boot",
            "could not read the kube unit; PI WEB may not come back after a reboot",
            path=path,
            error=str(e),
        )
        return
    restored = add_boot_install(current)
    if restored == current:
        jlog("info", "pi-web:boot", "kube unit already starts at boot", path=path)
        return
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(restored)
    except OSError as e:
        jlog(
            "error",
            "pi-web:boot",
            "could not rewrite the kube unit; PI WEB will not come back after a reboot",
            path=path,
            error=str(e),
        )
        return
    subprocess.run(
        ["systemctl", "--user", "daemon-reload"], check=False, capture_output=True
    )
    jlog("info", "pi-web:boot", "pi-web linked into default.target", path=path)


def start_pod() -> None:
    """Bring PI WEB up. A no-op for an already running pod, and the thing that
    ends the #1373 era on an upgraded box, where the service is left stopped."""
    subprocess.run(
        ["systemctl", "--user", "start", POD_UNIT], check=False, capture_output=True
    )
    jlog("info", "pi-web:boot", "PI WEB started", unit=POD_UNIT)


def main() -> int:
    data_dir = env("DATA_DIR", "/mnt/data/stacks")
    chat_port = env("CHAT_PORT", "8787")

    # The host half of the bridge, run by `pi-web-lease-broker.service`. It has
    # to come before anything else: ServiceBay executes this same file, and the
    # unit executes the copy of it.
    if len(sys.argv) > 1 and sys.argv[1] == "broker":
        return broker_run(data_dir, chat_port)

    write_models_json(data_dir, MODEL_GATE_PORT)
    retire_lease_unit(data_dir)
    install_broker_units(data_dir, chat_port, install_broker_script(data_dir))
    restore_boot_autostart()
    start_pod()
    release_own_lease(chat_port)
    return 0


if __name__ == "__main__":
    sys.exit(main())

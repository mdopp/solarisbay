#!/usr/bin/env python3
"""post-deploy hook for the `solaris` template — the Solaris Engine era.

The Hermes-era 2,800-line sequence (gateway config writer, profile
provisioning, boot hooks, cron registration, MCP block splicing, bundled-
skill opt-outs) is gone: the engine owns all of that in-process. What
remains is the wiring only a deploy can do:

  1. engine soul   — seed/sync SOUL.md on the chat-owned solaris-data volume
                     (#283 guard: an operator-edited soul is never clobbered).
  2. HA            — adopt the long-lived token (#1002, patches the pod yml);
                     auto-install the jellyfin integration (#195); wire the
                     VOICE PIPELINE: wyoming whisper + piper config entries,
                     the ollama-integration conversation agent pointing at the
                     engine's /ollama facade, the "Solaris" Assist pipeline
                     (create via the websocket storage API), set it preferred
                     and assign it to the Voice PE's pipeline select.
  3. admin MCP     — mint the servicebay_admin token (read+lifecycle+mutate,
                     no destroy/exec) and drop it at
                     <DATA_DIR>/solarisbay/sb-admin-token for the engine's admin
                     toolbox (read lazily — no restart needed).
  4. ONE restart   — POST /api/services/solaris/action {restart} as the LAST
                     step. Risk-2-safe (#271 spike): ServiceBay runs this
                     script in an SSH session and the restart is `--no-block`
                     async, so it does not kill the running post-deploy.

Every HA step is idempotent + fail-soft: a re-deploy converges, a missing
HA token skips the HA phase with a log line instead of failing the install.
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import json
import os
import re
import secrets as _secrets
import socket
import struct
import subprocess
import sys
import time
import urllib.error
import urllib.request

# A ServiceBay-minted MCP token is `sb_<8-hex-id>_<base32-ish-secret>`. Only
# this shape is accepted by ServiceBay's `/mcp` `verifyToken`; any other value
# is a permanent 401 (#126).
SB_MCP_TOKEN_RE = re.compile(r"^sb_[0-9a-f]{8}_[A-Z2-9]+$")

SOLARIS_SERVICE = "solaris"
CHAT_CONTAINER = os.environ.get("CHAT_CONTAINER", "solaris-chat")
GATEKEEPER_CONTAINER = os.environ.get("GATEKEEPER_CONTAINER", "solaris-gatekeeper")

ADMIN_TOKEN_NAME = "admin-soul"
ADMIN_MCP_SCOPES = ["read", "lifecycle", "mutate"]

PIPELINE_NAME = "Solaris"
CONVERSATION_AGENT_NAME = "Solaris"
ENGINE_MODEL = "solaris"
# HA's conversation subentry prompt — folded after the engine's own system
# block by the facade, so keep it to the voice-delivery essentials.
VOICE_PROMPT = "Antworte kurz, gesprochen und ohne Markdown."

HA_URL = "http://127.0.0.1:8123"


def env(key: str, default: str = "") -> str:
    val = os.environ.get(key, default)
    return val if val else default


def _truthy(val: str) -> bool:
    return val.strip().lower() in {"1", "true", "yes", "on"}


def jlog(level: str, tag: str, message: str, **args: object) -> None:
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


def emit_credential(**fields: object) -> None:
    sys.stdout.write("__SB_CREDENTIAL__ " + json.dumps(fields) + "\n")
    sys.stdout.flush()


def post_json(
    url: str, payload: dict[str, object], timeout: float = 10.0
) -> tuple[int, dict[str, object] | None]:
    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    token = os.environ.get("SB_API_TOKEN", "")
    if token:
        headers["X-SB-Internal-Token"] = token
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read().decode("utf-8")
            try:
                return resp.status, json.loads(data) if data else None
            except json.JSONDecodeError:
                return resp.status, None
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except Exception:  # pylint: disable=broad-except
            return e.code, None
    except (urllib.error.URLError, TimeoutError, OSError):
        return 0, None


def _container_env(container: str, name: str) -> str:
    """Read an env var from inside a running pod container — the rendered
    template value. The post-deploy runs in ServiceBay's context, which does
    NOT export the template variables to it, so the container is the source
    of truth. Returns '' if the container or var is unavailable."""
    try:
        proc = subprocess.run(
            ["podman", "exec", container, "printenv", name],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def chat_container_env(name: str) -> str:
    return _container_env(CHAT_CONTAINER, name)


def gatekeeper_container_env(name: str) -> str:
    return _container_env(GATEKEEPER_CONTAINER, name)


def selected_tts_voice(default: str = "martin") -> str:
    """The admin-selected global Kokoro voice (#368), persisted in the chat
    container's app_settings.json beside solaris.db. Read it from inside the
    container (the rendered DB path + the chat-owned volume). Empty/unset ⇒ the
    Martin default, so an install that never touched the picker is unchanged."""
    db_path = chat_container_env("SOLARIS_DB_PATH") or "/var/lib/solaris/solaris.db"
    settings_path = f"{os.path.dirname(db_path)}/app_settings.json"
    try:
        proc = subprocess.run(
            ["podman", "exec", CHAT_CONTAINER, "cat", settings_path],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return default
    if proc.returncode != 0:
        return default
    try:
        voice = json.loads(proc.stdout).get("tts_voice")
    except (ValueError, AttributeError):
        return default
    return voice.strip() if isinstance(voice, str) and voice.strip() else default


# ════════════════════════════════════════════════════════════════════════════
# 1. ENGINE SOUL — seed/sync SOUL.md on the chat-owned volume.
# ════════════════════════════════════════════════════════════════════════════


def _soul_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def write_engine_soul(data_dir: str) -> bool:
    """Seed/sync the Solaris Engine's SOUL.md on the chat-owned solarisbay volume.

    The engine reads the soul from `<data_dir>/solarisbay/SOUL.md` and the panel
    writes it directly. #283 guard: an operator-edited file is never
    clobbered; an unmodified shipped soul is updated when the pack ships a
    new one. Pure host-side file IO. Returns True when the file was written."""
    source = os.path.join(data_dir, "solaris", "skills", "household", "SOUL.md")
    target = os.path.join(data_dir, "solarisbay", "SOUL.md")
    marker = os.path.join(data_dir, "solarisbay", ".soul.shipped.sha256")
    try:
        with open(source, encoding="utf-8") as f:
            soul = f.read()
    except OSError:
        jlog("warn", "soul", "shipped SOUL.md not readable", source=source)
        return False
    existing = ""
    try:
        with open(target, encoding="utf-8") as f:
            existing = f.read()
    except OSError:
        pass
    if existing == soul:
        return False
    recorded = ""
    try:
        with open(marker, encoding="utf-8") as f:
            recorded = f.read().strip()
    except OSError:
        pass
    if existing.strip() and recorded and recorded != _soul_sha256(existing):
        jlog("info", "soul", "leaving operator-edited SOUL.md untouched", path=target)
        return False
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as f:
            f.write(soul)
        with open(marker, "w", encoding="utf-8") as f:
            f.write(_soul_sha256(soul) + "\n")
    except OSError as e:
        jlog("error", "soul", "could not write engine SOUL.md", error=str(e))
        return False
    jlog("info", "soul", "installed engine SOUL.md", path=target)
    return True


def ship_alarm_sound(data_dir: str) -> bool:
    """Copy the shipped default alarm tone into HA's media folder so
    `media-source://media_source/local/solaris-alarm.ogg` resolves and the
    Voice PE can ring it (#348). Host-side file IO; idempotent (skips when the
    target already matches). An operator's own tone in the media folder is
    never touched — only `solaris-alarm.ogg` is managed. Returns True when
    the file was written."""
    source = os.path.join(
        data_dir, "solaris", "skills", "household", "media", "solaris-alarm.ogg"
    )
    media_dir = os.path.join(data_dir, "home-assistant", "homeassistant", "media")
    target = os.path.join(media_dir, "solaris-alarm.ogg")
    try:
        with open(source, "rb") as f:
            tone = f.read()
    except OSError:
        jlog("warn", "alarm", "shipped alarm tone not readable", source=source)
        return False
    try:
        with open(target, "rb") as f:
            if f.read() == tone:
                return False
    except OSError:
        pass
    try:
        os.makedirs(media_dir, exist_ok=True)
        with open(target, "wb") as f:
            f.write(tone)
    except OSError as e:
        jlog("error", "alarm", "could not install alarm tone", error=str(e))
        return False
    jlog("info", "alarm", "installed default alarm tone", path=target)
    return True


# ════════════════════════════════════════════════════════════════════════════
# 2. HOME ASSISTANT — token adoption, jellyfin, the voice pipeline.
# ════════════════════════════════════════════════════════════════════════════


def _ha_token_timeout() -> int:
    # On a fresh install HA's auto-onboarding (servicebay#1847) writes
    # `.solaris-long-lived-token` only at the END of HA's first boot+onboard
    # run, which on a cold box runs past the old 90s window — the deploy then
    # adopted nothing and the engine came up with an empty HASS_TOKEN (#425).
    # 180s covers a cold first boot; an existing token is found on the first
    # poll so a re-deploy doesn't pay this.
    return int(os.environ.get("HA_TOKEN_TIMEOUT", "180"))


def _ha_api_timeout() -> int:
    return int(os.environ.get("HA_API_TIMEOUT", "60"))


def _wait_for_ha_token(token_path: str, deadline_secs: int | None = None) -> str | None:
    """#1002 — Poll for the HA long-lived token file HA's post-deploy writes
    near the end of its run. Returns the token once present + non-empty, or
    None at the deadline (0 = check once)."""
    if deadline_secs is None:
        deadline_secs = _ha_token_timeout()
    deadline = time.time() + deadline_secs
    while True:
        if os.path.exists(token_path):
            try:
                with open(token_path, encoding="utf-8") as f:
                    token = f.read().strip()
                if token:
                    return token
            except OSError:
                pass
        if time.time() >= deadline:
            return None
        time.sleep(3)


def _wait_for_ha_api(token: str, timeout_secs: int | None = None) -> bool:
    """Probe HA's /api/ with the token until it answers 200 (best-effort)."""
    if timeout_secs is None:
        timeout_secs = _ha_api_timeout()
    if timeout_secs <= 0:
        return False
    deadline = time.time() + timeout_secs
    while time.time() < deadline:
        status, _ = _ha_get("/api/", token, timeout=5)
        if 200 <= status < 300:
            return True
        time.sleep(3)
    jlog("warn", "ha", "HA /api/ not 200 within deadline", deadline_secs=timeout_secs)
    return False


def adopt_ha_long_lived_token(data_dir: str) -> str | None:
    """Pick up HA's auto-onboarded long-lived token (#934/#1002) and patch the
    deployed `solaris.yml` pod manifest's HASS_TOKEN env value so the engine
    can authenticate. Returns the token, or None when the file never appears."""
    token_path = os.path.join(
        data_dir, "home-assistant", "homeassistant", ".solaris-long-lived-token"
    )
    token = _wait_for_ha_token(token_path)
    if token is None:
        jlog(
            "warn",
            "ha",
            "no HA long-lived token to adopt — HASS_TOKEN stays empty, the engine "
            "cannot reach Home Assistant (no device control / Assist tool calls). "
            "ServiceBay's HA auto-onboarding (#934/#1002) writes this file; a fresh "
            "install with no migrated data won't have it until HA onboarding has run. "
            "Fix: (re)run HA auto-onboarding so it writes the token file, or drop a "
            "long-lived token there by hand, then redeploy Solaris to adopt it.",
            path=token_path,
        )
        return None
    pod_yml = os.path.expanduser("~/.config/containers/systemd/solaris.yml")
    if not os.path.exists(pod_yml):
        jlog("warn", "ha", "solaris.yml not found at expected path", path=pod_yml)
        return None
    try:
        with open(pod_yml, encoding="utf-8") as f:
            src = f.read()
    except OSError as e:
        jlog("warn", "ha", "could not read solaris.yml", path=pod_yml, error=str(e))
        return None
    new, n = re.subn(
        r'(- name: HASS_TOKEN\n\s+value: )(?:"[^"\n]*"|[^\n]*)',
        lambda m: m.group(1) + '"' + token + '"',
        src,
    )
    if n == 0:
        # The regex found no HASS_TOKEN env line to patch — adoption would
        # silently no-op and the engine would restart with an empty token. Fail
        # loudly instead of returning a token that never reaches the container.
        jlog(
            "warn",
            "ha",
            "HASS_TOKEN env entry not found in solaris.yml — token NOT adopted; "
            "the engine cannot reach Home Assistant. Check the chat container's "
            "HASS_TOKEN env block in templates/solaris/template.yml.",
            path=pod_yml,
        )
        return None
    if new != src:
        try:
            with open(pod_yml, "w", encoding="utf-8") as f:
                f.write(new)
        except OSError as e:
            jlog("warn", "ha", "could not write patched solaris.yml", error=str(e))
            return None
        jlog("info", "ha", "adopted HA long-lived token", token_path=token_path)
    else:
        jlog("info", "ha", "HASS_TOKEN already current in solaris.yml")
    _wait_for_ha_api(token)
    return token


def _ha_get(path: str, token: str, timeout: float = 10.0) -> tuple[int, object]:
    """GET against HA's API with the long-lived token. 0 on connection failure."""
    req = urllib.request.Request(
        f"{HA_URL}{path}", headers={"Authorization": f"Bearer {token}"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read().decode("utf-8")
            return resp.status, (json.loads(data) if data else None)
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except Exception:  # pylint: disable=broad-except
            return e.code, None
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
        return 0, None


def _ha_post(
    path: str, token: str, payload: dict[str, object], timeout: float = 30.0
) -> tuple[int, object]:
    """POST JSON against HA's API with the long-lived token. 0 on failure."""
    req = urllib.request.Request(
        f"{HA_URL}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read().decode("utf-8")
            return resp.status, (json.loads(data) if data else None)
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except Exception:  # pylint: disable=broad-except
            return e.code, None
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError):
        return 0, None


def _ha_request_delete(path: str, token: str, timeout: float = 10.0) -> None:
    """Best-effort DELETE against HA's API (used to abort a dangling flow)."""
    req = urllib.request.Request(
        f"{HA_URL}{path}",
        headers={"Authorization": f"Bearer {token}"},
        method="DELETE",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout):
            return
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError):
        return


def ensure_ha_jellyfin_integration(
    token: str, url: str, username: str, password: str
) -> bool:
    """Auto-install HA's `jellyfin` integration via HA's config-entries flow
    API (#195). Idempotent (skips if an entry exists) + fail-soft. Returns
    True only when a new entry was created."""
    if not (token and url and username):
        return False

    status, entries = _ha_get("/api/config/config_entries/entry", token)
    if status != 200 or not isinstance(entries, list):
        jlog("warn", "jellyfin", "could not list HA config entries", status=status)
        return False
    if any(isinstance(e, dict) and e.get("domain") == "jellyfin" for e in entries):
        jlog("info", "jellyfin", "HA jellyfin config entry already present")
        return False

    status, flow = _ha_post(
        "/api/config/config_entries/flow", token, {"handler": "jellyfin"}
    )
    if status != 200 or not isinstance(flow, dict) or not flow.get("flow_id"):
        jlog("warn", "jellyfin", "could not start jellyfin config flow", status=status)
        return False
    flow_id = flow["flow_id"]

    status, result = _ha_post(
        f"/api/config/config_entries/flow/{flow_id}",
        token,
        {"url": url, "username": username, "password": password},
    )
    if (
        status == 200
        and isinstance(result, dict)
        and result.get("type") == "create_entry"
    ):
        jlog("info", "jellyfin", "created HA jellyfin config entry", url=url)
        return True

    errors = result.get("errors") if isinstance(result, dict) else None
    _ha_request_delete(f"/api/config/config_entries/flow/{flow_id}", token)
    jlog("warn", "jellyfin", "jellyfin flow did not create an entry", errors=errors)
    return False


# ── voice pipeline ───────────────────────────────────────────────────────────


def wait_for_chat(chat_port: str, timeout_secs: int = 120) -> bool:
    """Wait for the chat server's /health — the ollama config flow validates
    against the engine facade, so the engine must be up first."""
    deadline = time.time() + timeout_secs
    url = f"http://127.0.0.1:{chat_port}/health"
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=5) as resp:
                if resp.status == 200:
                    return True
        except (urllib.error.URLError, TimeoutError, OSError):
            pass
        time.sleep(3)
    jlog("warn", "voice", "chat /health not up within deadline", port=chat_port)
    return False


def _port_open(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _wait_for_port(host: str, port: int, timeout_secs: int = 60) -> bool:
    """Poll a TCP port until it accepts a connection or the deadline passes.

    The gatekeeper's Wyoming STT listener (:10700) boots a few seconds after
    the post-deploy starts wiring (box-observed ~7s, #395), so its entity must
    not be registered before the port answers — otherwise the STT converge
    silently no-ops and speaker-ID never enters the live path."""
    deadline = time.time() + timeout_secs
    while True:
        if _port_open(host, port):
            return True
        if time.time() >= deadline:
            return False
        time.sleep(3)


def _flow_create(
    token: str, handler: str, steps: list[dict[str, object]]
) -> tuple[str, dict | None]:
    """Drive one HA config flow: start it, submit each step's data in order.

    Returns ("created", entry) | ("already", None) | ("failed", last_result).
    """
    status, flow = _ha_post(
        "/api/config/config_entries/flow", token, {"handler": handler}
    )
    if status != 200 or not isinstance(flow, dict) or not flow.get("flow_id"):
        return "failed", flow if isinstance(flow, dict) else None
    result: dict | None = flow
    for step in steps:
        flow_id = result.get("flow_id") if isinstance(result, dict) else None
        if not flow_id:
            break
        status, result = _ha_post(
            f"/api/config/config_entries/flow/{flow_id}", token, step
        )
        if not isinstance(result, dict):
            return "failed", None
        if result.get("type") == "abort":
            reason = str(result.get("reason") or "")
            if "already" in reason:
                return "already", None
            return "failed", result
    if isinstance(result, dict) and result.get("type") == "create_entry":
        return "created", result.get("result") if isinstance(
            result.get("result"), dict
        ) else result
    if isinstance(result, dict) and (fid := result.get("flow_id")):
        _ha_request_delete(f"/api/config/config_entries/flow/{fid}", token)
    return "failed", result


def ensure_wyoming_entry(token: str, label: str, host: str, port: int) -> None:
    """Register a wyoming service (whisper STT / piper TTS) in HA. The wyoming
    flow does NOT de-dup on host+port (box-observed: a second run created a
    duplicate entry), so idempotency is a title check against the existing
    entries — the entry title is the announced wyoming service name."""
    status, entries = _ha_get("/api/config/config_entries/entry", token)
    if status == 200 and isinstance(entries, list):
        for e in entries:
            if (
                isinstance(e, dict)
                and e.get("domain") == "wyoming"
                and label in str(e.get("title") or "").lower()
            ):
                jlog("info", "voice", f"wyoming {label}: already", port=port)
                return
    state, result = _flow_create(token, "wyoming", [{"host": host, "port": port}])
    jlog(
        "info" if state in ("created", "already") else "warn",
        "voice",
        f"wyoming {label}: {state}",
        host=host,
        port=port,
        detail=(result or {}).get("reason") if state == "failed" else None,
    )


def _ollama_entry_id(token: str, facade_url: str) -> str:
    """The entry_id of the ollama config entry pointing at the engine facade,
    or ''. Falls back to any ollama entry whose title carries the facade URL."""
    status, entries = _ha_get("/api/config/config_entries/entry", token)
    if status != 200 or not isinstance(entries, list):
        return ""
    candidates = [
        e for e in entries if isinstance(e, dict) and e.get("domain") == "ollama"
    ]
    for e in candidates:
        if facade_url in str(e.get("title") or ""):
            return str(e.get("entry_id") or "")
    if len(candidates) == 1:
        return str(candidates[0].get("entry_id") or "")
    return ""


def ensure_conversation_agent(token: str, chat_port: str, api_key: str) -> str:
    """Wire Solaris as an HA conversation agent: an `ollama` config entry pointing
    at the engine's /ollama facade + a `conversation` subentry on model `solaris`.

    HA 2026.6's openai_conversation has no custom base_url; its ollama
    integration takes a free URL + Bearer api_key and speaks exactly the
    facade's protocol (box-verified 2026-06-12). Returns the conversation
    entity_id, or ''.
    """
    facade_url = f"http://127.0.0.1:{chat_port}/ollama"
    state, result = _flow_create(
        token, "ollama", [{"url": facade_url, "api_key": api_key}]
    )
    if state == "failed":
        jlog("warn", "voice", "ollama entry flow failed", detail=result)
        return ""
    entry_id = ""
    if state == "created" and isinstance(result, dict):
        entry_id = str(result.get("entry_id") or "")
    if not entry_id:
        entry_id = _ollama_entry_id(token, facade_url)
    if not entry_id:
        jlog("warn", "voice", "no ollama entry id resolvable")
        return ""

    # Conversation entity already there? (idempotent re-deploy)
    existing = _find_entity(token, "conversation.", CONVERSATION_AGENT_NAME.lower())
    if existing:
        return existing

    # A freshly-created entry loads asynchronously and the subentry flow
    # aborts `entry_not_loaded` until it has — retry briefly.
    result: dict | None = None
    for attempt in range(5):
        if attempt:
            time.sleep(3)
        status, flow = _ha_post(
            "/api/config/config_entries/subentries/flow",
            token,
            {"handler": [entry_id, "conversation"]},
        )
        if status != 200 or not isinstance(flow, dict) or not flow.get("flow_id"):
            jlog(
                "warn", "voice", "conversation subentry flow not started", status=status
            )
            return ""
        status, result = _ha_post(
            f"/api/config/config_entries/subentries/flow/{flow['flow_id']}",
            token,
            {
                "name": CONVERSATION_AGENT_NAME,
                "model": ENGINE_MODEL,
                "prompt": VOICE_PROMPT,
            },
        )
        if (
            status == 200
            and isinstance(result, dict)
            and result.get("type") == "create_entry"
        ):
            break
        if isinstance(result, dict) and result.get("reason") == "entry_not_loaded":
            continue
        break
    if not isinstance(result, dict) or result.get("type") != "create_entry":
        jlog("warn", "voice", "conversation subentry not created", detail=result)
        return ""
    jlog("info", "voice", "created Solaris conversation agent", entry_id=entry_id)
    # The conversation entity registers asynchronously — poll briefly.
    for _ in range(10):
        entity = _find_entity(token, "conversation.", CONVERSATION_AGENT_NAME.lower())
        if entity:
            return entity
        time.sleep(2)
    return ""


def _find_entity(token: str, prefix: str, needle: str = "") -> str:
    """First entity_id with `prefix` (and `needle` in the id or friendly
    name), or ''."""
    status, states = _ha_get("/api/states", token)
    if status != 200 or not isinstance(states, list):
        return ""
    for s in states:
        if not isinstance(s, dict):
            continue
        entity_id = str(s.get("entity_id") or "")
        if not entity_id.startswith(prefix):
            continue
        friendly = str((s.get("attributes") or {}).get("friendly_name") or "")
        if not needle or needle in entity_id.lower() or needle in friendly.lower():
            return entity_id
    return ""


class HAWebSocket:
    """Minimal RFC6455 client for HA's /api/websocket (stdlib only).

    Only what the pipeline storage API needs: auth, send command, await its
    result. The assist_pipeline collection has no REST surface — websocket is
    the only way to create a pipeline."""

    def __init__(self, token: str, host: str = "127.0.0.1", port: int = 8123):
        self._token = token
        self._sock = socket.create_connection((host, port), timeout=15)
        self._sock.settimeout(15)
        self._buf = b""
        self._next_id = 1
        self._handshake(host)
        self._auth()

    def _handshake(self, host: str) -> None:
        key = base64.b64encode(_secrets.token_bytes(16)).decode()
        self._sock.sendall(
            (
                f"GET /api/websocket HTTP/1.1\r\nHost: {host}\r\n"
                "Upgrade: websocket\r\nConnection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
            ).encode()
        )
        response = b""
        while b"\r\n\r\n" not in response:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise ConnectionError("websocket handshake EOF")
            response += chunk
        if b" 101 " not in response.split(b"\r\n", 1)[0]:
            raise ConnectionError("websocket upgrade refused")
        self._buf = response.split(b"\r\n\r\n", 1)[1]

    def _auth(self) -> None:
        msg = self._recv_json()
        if msg.get("type") != "auth_required":
            raise ConnectionError(f"unexpected hello: {msg}")
        self._send_json({"type": "auth", "access_token": self._token})
        msg = self._recv_json()
        if msg.get("type") != "auth_ok":
            raise ConnectionError(f"auth failed: {msg}")

    def cmd(self, payload: dict[str, object]) -> dict:
        """Send one command; return its result message (raises on error)."""
        msg_id = self._next_id
        self._next_id += 1
        self._send_json({"id": msg_id, **payload})
        while True:
            msg = self._recv_json()
            if msg.get("id") == msg_id and msg.get("type") == "result":
                if not msg.get("success"):
                    raise RuntimeError(f"HA command failed: {msg.get('error')}")
                return msg.get("result") or {}

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass

    # -- frames ---------------------------------------------------------------

    def _send_json(self, obj: dict[str, object]) -> None:
        payload = json.dumps(obj).encode()
        mask = _secrets.token_bytes(4)
        length = len(payload)
        if length < 126:
            header = struct.pack("!BB", 0x81, 0x80 | length)
        elif length < 1 << 16:
            header = struct.pack("!BBH", 0x81, 0x80 | 126, length)
        else:
            header = struct.pack("!BBQ", 0x81, 0x80 | 127, length)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self._sock.sendall(header + mask + masked)

    def _read_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self._sock.recv(65536)
            if not chunk:
                raise ConnectionError("websocket EOF")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def _recv_json(self) -> dict:
        message = b""
        while True:
            b1, b2 = self._read_exact(2)
            opcode = b1 & 0x0F
            length = b2 & 0x7F
            if length == 126:
                (length,) = struct.unpack("!H", self._read_exact(2))
            elif length == 127:
                (length,) = struct.unpack("!Q", self._read_exact(8))
            payload = self._read_exact(length)
            if opcode == 0x9:  # ping -> pong
                self._sock.sendall(struct.pack("!BB", 0x8A, 0x80) + b"\x00" * 4)
                continue
            if opcode == 0x8:
                raise ConnectionError("websocket closed by server")
            message += payload
            if b1 & 0x80:  # FIN
                return json.loads(message.decode("utf-8"))


def ensure_assist_pipeline(
    token: str, conversation_entity: str, prefer_gatekeeper_stt: bool = False
) -> bool:
    """Create the "Solaris" Assist pipeline (wake on-device, stt=whisper,
    conversation=Solaris, tts=piper) and make it the preferred pipeline. Then
    point the Voice PE's pipeline select at it. Idempotent on the name.

    With `prefer_gatekeeper_stt` (speaker-ID on, #350) the STT engine is the
    gatekeeper's Wyoming STT entity instead of bare whisper, so the pipeline's
    audio flows through the gatekeeper's speaker-ID resolver. It transcribes
    via whisper internally, so STT output is identical — only the resident is
    additionally resolved and stashed for the engine facade."""
    # Needle-match the wyoming engines: the box already carries other tts
    # entities (e.g. a google cloud one) and the pipeline must ride the
    # local whisper/piper pair. Fresh wyoming entries register their
    # entities asynchronously — poll briefly.
    stt_entity = tts_entity = ""
    for attempt in range(10):
        if attempt:
            time.sleep(3)
        if prefer_gatekeeper_stt:
            # The gatekeeper registers a Wyoming STT entity from its ASR
            # program (name solaris-gatekeeper-asr); needle "gatekeeper".
            # Fall back to whisper if it hasn't materialised yet.
            stt_entity = _find_entity(token, "stt.", "gatekeeper") or _find_entity(
                token, "stt.", "whisper"
            )
        else:
            stt_entity = _find_entity(token, "stt.", "whisper") or _find_entity(
                token, "stt.", "wyoming"
            )
        # Solaris's Martin voice when its bridge entity exists (GPU boxes,
        # servicebay#1815); piper otherwise. The two differ in language/
        # voice fields: the bridge announces plain `de` + voice `kokoro`,
        # piper needs the regional `de_DE` (a bare `de` 500s announces).
        tts_entity = (
            _find_entity(token, "tts.", "openai")
            or _find_entity(token, "tts.", "piper")
            or _find_entity(token, "tts.", "wyoming")
        )
        if stt_entity and tts_entity:
            break
    if not (stt_entity and tts_entity and conversation_entity):
        jlog(
            "warn",
            "voice",
            "pipeline prerequisites missing",
            stt=stt_entity,
            tts=tts_entity,
            conversation=conversation_entity,
        )
        return False
    try:
        ws = HAWebSocket(token)
    except (OSError, ConnectionError, RuntimeError) as e:
        jlog("warn", "voice", "HA websocket unavailable", error=str(e))
        return False
    try:
        listed = ws.cmd({"type": "assist_pipeline/pipeline/list"})
        pipelines = listed.get("pipelines") or []
        existing = next(
            (
                p
                for p in pipelines
                if isinstance(p, dict) and p.get("name") == PIPELINE_NAME
            ),
            None,
        )
        martin = "openai" in tts_entity
        tts_fields = {
            "tts_engine": tts_entity,
            # The Martin bridge announces plain `de` + voice `kokoro`; wyoming
            # piper announces regional voice codes — there a bare "de" makes
            # every announce/TTS call 500 with "Language 'de' not supported"
            # (box-verified 2026-06-12).
            "tts_language": "de" if martin else "de_DE",
            # The bridge announces a Kokoro VOICE name (the model name on the
            # OpenAI side is "kokoro" — wrong here: a kokoro tts_voice makes
            # every pipeline TTS fail silently, box bridge log 2026-06-12). The
            # admin-selected voice (#368) converges here; default Martin.
            "tts_voice": selected_tts_voice() if martin else None,
        }
        if existing is None:
            created = ws.cmd(
                {
                    "type": "assist_pipeline/pipeline/create",
                    "name": PIPELINE_NAME,
                    "language": "de",
                    "conversation_engine": conversation_entity,
                    "conversation_language": "de",
                    "stt_engine": stt_entity,
                    "stt_language": "de",
                    **tts_fields,
                    "wake_word_entity": None,
                    "wake_word_id": None,
                }
            )
            pipeline_id = created.get("id")
            jlog("info", "voice", "created Solaris assist pipeline", id=pipeline_id)
        else:
            pipeline_id = existing.get("id")
            # Converge an existing pipeline onto the preferred TTS (a GPU box
            # may have been wired with piper before the Martin units landed)
            # and onto the preferred STT (speaker-ID toggled on a redeploy
            # moves it from whisper to the gatekeeper, #350).
            if (
                existing.get("tts_engine") != tts_entity
                or existing.get("tts_voice") != tts_fields["tts_voice"]
                or existing.get("stt_engine") != stt_entity
            ):
                upd = {
                    k: existing.get(k)
                    for k in (
                        "conversation_engine",
                        "conversation_language",
                        "language",
                        "name",
                        "stt_engine",
                        "stt_language",
                        "tts_engine",
                        "tts_language",
                        "tts_voice",
                        "wake_word_entity",
                        "wake_word_id",
                    )
                }
                upd.update(tts_fields)
                upd["stt_engine"] = stt_entity
                ws.cmd(
                    {
                        "type": "assist_pipeline/pipeline/update",
                        "pipeline_id": pipeline_id,
                        **upd,
                    }
                )
                jlog(
                    "info",
                    "voice",
                    "Solaris pipeline converged",
                    tts=tts_entity,
                    stt=stt_entity,
                )
            else:
                jlog("info", "voice", "Solaris assist pipeline already present")
        if pipeline_id:
            ws.cmd(
                {
                    "type": "assist_pipeline/pipeline/set_preferred",
                    "pipeline_id": pipeline_id,
                }
            )
    except (OSError, ConnectionError, RuntimeError) as e:
        jlog("warn", "voice", "pipeline create/set_preferred failed", error=str(e))
        return False
    finally:
        ws.close()

    _assign_pe_pipeline(token)
    return True


def _assign_pe_pipeline(token: str) -> None:
    """Point the Voice PE's pipeline select(s) at the Solaris pipeline (fail-soft
    — a select on `preferred` already follows the preferred pipeline, this
    just pins it explicitly; the box PE exposes two assistant selects)."""
    status, states = _ha_get("/api/states", token)
    if status != 200 or not isinstance(states, list):
        return
    selects = [
        str(s.get("entity_id"))
        for s in states
        if isinstance(s, dict)
        and str(s.get("entity_id") or "").startswith("select.")
        and "voice" in str(s.get("entity_id"))
        and "assist" in str(s.get("entity_id"))
    ]
    if not selects:
        jlog("info", "voice", "no PE pipeline select entity found — skipping assign")
        return
    for select in selects:
        status, _ = _ha_post(
            "/api/services/select/select_option",
            token,
            {"entity_id": select, "option": PIPELINE_NAME},
        )
        jlog(
            "info" if status == 200 else "warn",
            "voice",
            "PE pipeline select",
            entity=select,
            option=PIPELINE_NAME,
            status=status,
        )


def wire_voice_pipeline(token: str, chat_port: str, api_key: str) -> None:
    """The Phase-2 wiring: wyoming STT/TTS + conversation agent + pipeline."""
    if not token:
        jlog("info", "voice", "no HA token — skipping voice pipeline wiring")
        return
    ensure_wyoming_entry(token, "whisper", "127.0.0.1", 10300)
    ensure_wyoming_entry(token, "piper", "127.0.0.1", 10200)
    # Solaris's Martin voice (servicebay#1815): the wyoming_openai bridge only
    # runs on GPU boxes — register it when it listens, skip silently when not.
    if _port_open("127.0.0.1", 10203):
        ensure_wyoming_entry(token, "openai", "127.0.0.1", 10203)
    # Speaker-ID on the live path (#350, approach b): register the gatekeeper
    # as a Wyoming STT engine so the Assist pipeline's audio flows through it
    # — it transcribes, resolves the speaking resident, and stashes the uid
    # for the engine facade. The gatekeeper listens on its Wyoming port (the
    # same listener satellites use), reachable over host loopback. TTS stays
    # piper/Martin and the conversation agent stays Solaris.
    #
    # The flag + port live on the gatekeeper container: SB does NOT export the
    # template vars to this script (see _container_env), so read them from the
    # container, falling back to a process env / default for local runs.
    speaker_id = _truthy(
        env("SOLARIS_SPEAKER_ID_ENABLED")
        or gatekeeper_container_env("SOLARIS_SPEAKER_ID_ENABLED")
    )
    if speaker_id:
        gk_port = int(
            env("GATEKEEPER_PORT")
            or gatekeeper_container_env("GATEKEEPER_PORT")
            or "10700"
        )
        # The gatekeeper's Wyoming STT listener boots ~7s after this wiring runs
        # (#395); register it only once :gk_port answers, else the STT converge
        # silently no-ops and speaker-ID stays out of the live STT path.
        if _wait_for_port("127.0.0.1", gk_port):
            ensure_wyoming_entry(token, "gatekeeper", "127.0.0.1", gk_port)
        else:
            jlog(
                "warn",
                "voice",
                "gatekeeper Wyoming STT not reachable — skipping STT rewire",
                port=gk_port,
            )
            speaker_id = False
    if not wait_for_chat(chat_port):
        jlog("warn", "voice", "engine facade not up — conversation agent skipped")
        return
    conversation_entity = ensure_conversation_agent(token, chat_port, api_key)
    if conversation_entity:
        ensure_assist_pipeline(
            token, conversation_entity, prefer_gatekeeper_stt=speaker_id
        )


# ════════════════════════════════════════════════════════════════════════════
# 3. ADMIN MCP TOKEN — minted via the SB API, dropped as a file the engine
#    reads lazily.
# ════════════════════════════════════════════════════════════════════════════


def probe_admin_token(token: str, mcp_url: str) -> bool:
    """Live-validate an admin bearer against `/mcp`. 200 = ok; 401 = stale.
    Connection failure returns True (don't churn tokens on a hiccup)."""
    if not token or not mcp_url:
        return False
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "solaris-post-deploy", "version": "1"},
        },
    }
    req = urllib.request.Request(
        mcp_url, data=json.dumps(payload).encode("utf-8"), method="POST"
    )
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json, text/event-stream")
    req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return 200 <= resp.status < 300
    except urllib.error.HTTPError as e:
        return e.code != 401
    except (urllib.error.URLError, ConnectionError, TimeoutError, OSError):
        return True


def mint_admin_token(
    sb_api: str, attempts: int = 4, backoff_s: float = 3.0
) -> str | None:
    """Mint a read+lifecycle+mutate ServiceBay-MCP token for the admin
    persona. Retries for the SB readiness race (#126); never persists a
    non-`sb_` fallback."""
    for attempt in range(1, attempts + 1):
        status, body = post_json(
            f"{sb_api}/api/system/api-tokens",
            {"name": ADMIN_TOKEN_NAME, "scopes": ADMIN_MCP_SCOPES},
            timeout=15,
        )
        if status == 200 and isinstance(body, dict):
            secret = body.get("secret")
            if isinstance(secret, str) and SB_MCP_TOKEN_RE.match(secret):
                jlog("info", "admin-mcp", "minted admin SB-MCP token", attempt=attempt)
                return secret
        if attempt < attempts:
            time.sleep(backoff_s)
    jlog("warn", "admin-mcp", "could not mint admin SB-MCP token", attempts=attempts)
    return None


def ensure_admin_token_file(data_dir: str, sb_api: str, mcp_url: str) -> bool:
    """Keep a live admin token at <data_dir>/solarisbay/sb-admin-token (0600).
    The engine's admin toolbox reads it per connection, so a token minted
    here works without a restart. An existing token that still probes OK is
    kept (don't churn SB's token list)."""
    path = os.path.join(data_dir, "solarisbay", "sb-admin-token")
    existing = ""
    try:
        with open(path, encoding="utf-8") as f:
            existing = f.read().strip()
    except OSError:
        pass
    if (
        existing
        and SB_MCP_TOKEN_RE.match(existing)
        and probe_admin_token(existing, mcp_url)
    ):
        jlog("info", "admin-mcp", "existing admin token still valid")
        return True
    token = mint_admin_token(sb_api)
    if not token:
        return False
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(token + "\n")
        os.chmod(path, 0o600)
    except OSError as e:
        jlog("error", "admin-mcp", "could not write admin token file", error=str(e))
        return False
    jlog("info", "admin-mcp", "wrote admin token file", path=path)
    return True


# ════════════════════════════════════════════════════════════════════════════
# 4. RESTART — last step.
# ════════════════════════════════════════════════════════════════════════════


def restart_solaris(sb_api: str) -> bool:
    """POST /api/services/solaris/action {action: 'restart'} so the chat
    container picks up the patched HASS_TOKEN. Risk-2-safe (#271 spike): SB
    runs this script in an SSH session and the restart is `--no-block` async,
    so the queued restart does not kill the running post-deploy."""
    status, body = post_json(
        f"{sb_api}/api/services/{SOLARIS_SERVICE}/action",
        {"action": "restart"},
        timeout=30,
    )
    if status == 200:
        jlog("info", "restart", "restart requested via ServiceBay API")
        return True
    err = (body or {}).get("error") if isinstance(body, dict) else None
    jlog("warn", "restart", "restart request failed", status=status, error=str(err))
    return False


# ════════════════════════════════════════════════════════════════════════════
# main — the ordered sequence.
# ════════════════════════════════════════════════════════════════════════════


def main() -> int:
    data_dir = env("DATA_DIR", "/mnt/data")
    sb_api = env("SB_API_URL", "http://localhost:3000").rstrip("/")
    host = env("HOST", "<server-ip>")
    chat_port = env("CHAT_PORT") or chat_container_env("CHAT_PORT") or "8787"
    api_key = env("SOLARIS_API_KEY") or chat_container_env("SOLARIS_API_KEY")
    mcp_url = (
        env("SERVICEBAY_MCP_URL")
        or chat_container_env("SB_MCP_URL")
        or "http://127.0.0.1:5888/mcp"
    )

    # ── 1. engine soul ───────────────────────────────────────────────────────
    write_engine_soul(data_dir)

    # ── 2. Home Assistant ────────────────────────────────────────────────────
    ship_alarm_sound(data_dir)
    ha_token = adopt_ha_long_lived_token(data_dir)
    if ha_token:
        ensure_ha_jellyfin_integration(
            ha_token,
            env("JELLYFIN_URL"),
            env("JELLYFIN_USERNAME"),
            env("JELLYFIN_PASSWORD"),
        )
        wire_voice_pipeline(ha_token, chat_port, api_key)

    # ── 3. admin MCP token ───────────────────────────────────────────────────
    ensure_admin_token_file(data_dir, sb_api, mcp_url)

    # ── 4. restart ───────────────────────────────────────────────────────────
    time.sleep(3)
    restart_solaris(sb_api)

    if api_key:
        emit_credential(
            service="Solaris (Solaris Engine API)",
            url=f"http://{host}:{chat_port}/ollama",
            username="(bearer token)",
            password=api_key,
            importance="critical",
            notes="Bearer for the engine's Ollama-compatible facade (HA conversation agent + gatekeeper). Send as `Authorization: Bearer <key>`.",
        )

    print(f"✅ Solaris is configured: Solaris Engine on port {chat_port}.")
    print("   Chat surface + gatekeeper voice bridge run in the same Pod;")
    print("   the Voice PE rides HA's Assist pipeline into the engine.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

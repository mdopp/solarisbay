#!/usr/bin/env python3
"""post-deploy hook for the merged `solilos` template (#271).

One ordered script sequencing the four former post-deploys (hermes +
solilos-chat + solbay + admin-soul) now that they are one Pod / one
ServiceBay service. The logic is the SAME as the four standalone scripts —
re-sequenced, not rewritten — so every box-proven behaviour is preserved:

  1. hermes phase  — write config.yaml (model block + #265 timezone + #268
     disabled toolsets) → splice the SB-MCP servicebay entry (#1045) →
     install the Solilos SOUL.md (#283 sidecar-hash guard, #276 grounding,
     #266 paren-free) → adopt HA's long-lived token (#1002) → auto-install
     the HA jellyfin integration (#195) → merge messaging-gateway .env →
     write the ddgs install script. (NO restart here — see step 5.)
  2. chat phase    — decommission the retired open-webui / hermes-webui pods
     (#139/#140).
  3. solbay phase  — wait_for_hermes → register the periodic crons (#83/#182/
     #210) → collect + merge the household mcp_servers block (servicebay-mcp +
     gatekeeper-mcp), read/written through `podman exec solilos-hermes`.
  4. admin-soul    — splice the operator `servicebay_admin` (read+lifecycle+
     mutate) mcp entry through `podman exec solilos-hermes`, leaving the
     household entries untouched (#175).
  5. ONE restart   — POST /api/services/solilos/action {action: restart} as
     the LAST step so the containers pick up the final config.yaml + .env.
     Risk-2-safe per the #271 box spike: ServiceBay runs this script in an
     SSH session (not the unit's cgroup) and `startService` uses
     `systemctl --user --no-block restart` — the queued async restart does
     not kill the running post-deploy.

The host file <DATA_DIR>/hermes/config.yaml is owned by the hermes user
(uid 10000 → a host subuid, mode 640) once the hermes container has chowned
its data volume, so the solbay + admin phases read/write it through the
`solilos-hermes` container (where /opt/data is its home), same as the two
standalone scripts did via `hermes-hermes`.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

# A ServiceBay-minted MCP token is `sb_<8-hex-id>_<base32-ish-secret>`. Only
# this shape is accepted by ServiceBay's `/mcp` `verifyToken`; any other value
# is a permanent 401 (#126). Used to refuse a junk token and to self-heal a box
# that already wrote one.
SB_MCP_TOKEN_RE = re.compile(r"^sb_[0-9a-f]{8}_[A-Z2-9]+$")
SB_MCP_URL = "http://127.0.0.1:5888/mcp"

# The merged ServiceBay service name (#271). The config-agent's model-switch
# restart, this script's final restart, and the SB-API service action all
# target it.
SOLILOS_SERVICE = "solilos"

# The host config.yaml is owned by the hermes user (mode 640); the solbay +
# admin phases read/write it through the hermes container, where /opt/data is
# config.yaml's home. With the merged Pod named `solilos`, the hermes
# container is `solilos-hermes` (podman pod-container = `<pod>-<container>`).
HERMES_CONTAINER = os.environ.get("HERMES_CONTAINER", "solilos-hermes")
CONTAINER_CONFIG_PATH = "/opt/data/config.yaml"


def env(key: str, default: str = "") -> str:
    val = os.environ.get(key, default)
    return val if val else default


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


# ════════════════════════════════════════════════════════════════════════════
# 1. HERMES PHASE — config.yaml, SB-MCP entry, SOUL.md, HA token, jellyfin,
#    gateway .env, ddgs install script. (Was templates/hermes/post-deploy.py;
#    the restart is deferred to the single final step.)
# ════════════════════════════════════════════════════════════════════════════


def _honcho_health_timeout() -> int:
    return int(os.environ.get("HONCHO_PROBE_TIMEOUT", "5"))


def detect_honcho(port: str) -> bool:
    """#1004 — Probe http://127.0.0.1:<HONCHO_PORT>/health. Returns True
    when reachable + 2xx, False otherwise. Short timeout: the honcho
    template's own post-deploy already waited for /health, so by the
    time we run we either get an immediate green answer or accept that
    honcho isn't installed."""
    timeout = _honcho_health_timeout()
    if timeout <= 0 or not port:
        return False
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/health")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError):
        return False


def enumerate_ollama_tags(provider_url: str) -> list[str]:
    """Query the Ollama HTTP API for the list of installed model tags.

    `provider_url` is the OpenAI-compatible base (e.g.
    `http://127.0.0.1:11434/v1`). Ollama's native list endpoint lives at
    `<host>/api/tags`, one level up from the OpenAI surface — strip the
    trailing `/v1` if present.

    Returns a list of `name:tag` strings, or `[]` on any failure (caller
    falls back to leaving custom_providers.models empty, which Hermes then
    auto-detects via /v1/models — slower but functional).
    """
    if not provider_url:
        return []
    base = provider_url.rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3]
    try:
        with urllib.request.urlopen(f"{base}/api/tags", timeout=8) as resp:
            payload = json.loads(resp.read().decode("utf-8") or "{}")
    except (
        urllib.error.URLError,
        urllib.error.HTTPError,
        TimeoutError,
        OSError,
        json.JSONDecodeError,
    ) as e:
        jlog(
            "warn",
            "hermes:ollama-tags",
            "could not enumerate ollama tags — Hermes' Models tab will fall back to auto-detect via /v1/models",
            error=str(e),
        )
        return []
    tags: list[str] = []
    for entry in payload.get("models", []) or []:
        name = entry.get("name")
        if isinstance(name, str) and name:
            tags.append(name)
    return sorted(set(tags))


def render_custom_providers_block(provider_url: str, tags: list[str]) -> str:
    """Build a YAML `custom_providers:` block listing every Ollama tag on the
    host under a single `ollama` named provider, so Hermes' dashboard Models
    tab surfaces them as one-click switches. Returns '' if there are no tags
    (an empty block is YAML-invalid)."""
    if not tags or not provider_url:
        return ""
    out = [
        "custom_providers:\n",
        "  - name: ollama\n",
        f"    base_url: {provider_url}\n",
        '    api_key: "none"\n',
        "    models:\n",
    ]
    for tag in tags:
        # Ollama tags only use `:` as a name/tag separator (`gemma4:12b`); no
        # space follows, so the key is safe to emit unquoted. `{}` = no overrides.
        out.append(f"      {tag}: {{}}\n")
    return "".join(out)


def _extract_top_level_block(content: str, key: str) -> str:
    """Extract a top-level YAML block by key, returning the block (header +
    indented body), or '' if absent. "Top-level" = key at column 0; block ends
    at the next top-level key or EOF."""
    if not content or not key:
        return ""
    out: list[str] = []
    in_block = False
    for line in content.splitlines(keepends=True):
        stripped = line.lstrip()
        if not in_block:
            if line[:1] not in (" ", "\t") and stripped.startswith(f"{key}:"):
                in_block = True
                out.append(line)
            continue
        if line.strip() == "" or line[:1] in (" ", "\t"):
            out.append(line)
        else:
            break
    return "".join(out)


def write_config_yaml(
    data_dir: str,
    provider_url: str,
    model: str,
    honcho_port: str = "",
    honcho_api_key: str = "",
) -> str | None:
    """Write /opt/data/config.yaml's model: block (host path). Returns the path
    on success, None on write failure. Preserves an existing `mcp_servers:`
    block (#1045) so the SB-MCP / household entries survive a redeploy."""
    config_dir = os.path.join(data_dir, "hermes")
    config_path = os.path.join(config_dir, "config.yaml")
    preserved_mcp_servers = ""
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                preserved_mcp_servers = _extract_top_level_block(
                    f.read(), "mcp_servers"
                )
        except OSError as e:
            jlog(
                "warn",
                "hermes:config",
                "could not read existing config.yaml for mcp_servers preservation",
                path=config_path,
                error=str(e),
            )
    try:
        os.makedirs(config_dir, exist_ok=True)
    except OSError as e:
        jlog(
            "error",
            "hermes:config",
            "could not create config dir",
            path=config_dir,
            error=str(e),
        )
        return None
    # #1002 — ServiceBay defaults table; six overrides on top of Hermes'
    # upstream defaults that swing the box toward "household appliance".
    honcho_ready = bool(honcho_port) and detect_honcho(honcho_port)
    if honcho_ready:
        jlog(
            "info",
            "hermes:config",
            "honcho /health reachable — memory.provider=honcho",
            port=honcho_port,
        )
        memory_block = (
            "memory:\n"
            "  provider: honcho\n"
            "  honcho:\n"
            f"    api_url: http://127.0.0.1:{honcho_port}\n"
            f'    api_key: "{honcho_api_key}"\n'
        )
    else:
        memory_block = "memory:\n  provider: holographic\n"
    ollama_tags = enumerate_ollama_tags(provider_url)
    custom_providers_block = render_custom_providers_block(provider_url, ollama_tags)

    content = (
        "# Written by ServiceBay's solilos template post-deploy.py.\n"
        "# Edit via the wizard's reconfigure flow or hand-edit and restart the solilos service.\n"
        # Local time zone (#265) — Hermes stamps each session's "Conversation
        # started" line and times tool runs against this. Empty leaves the
        # container on UTC. Single-locale household; mirror the other defaults.
        "timezone: Europe/Berlin\n"
        "model:\n"
        f"  provider: custom\n"
        f"  model: {model}\n"
        f"  base_url: {provider_url}\n"
        f'  api_key: "none"\n' + memory_block + "tts:\n"
        "  provider: piper\n"
        "browser:\n"
        "  engine: disabled\n"
        "model_catalog:\n"
        "  enabled: false\n"
        "network:\n"
        "  force_ipv4: true\n"
        "display:\n"
        "  personality: default\n"
        # Reasoning surfaced PER REQUEST by the proxies (#222/#224); leave the
        # global default off so the common fast turn stays clean.
        "  show_reasoning: false\n"
        # Cold-cache prefill trim (#230 + #268 latency bundle): disable only the
        # clearly-unused dev/external/generation toolsets. cronjob STAYS enabled
        # (load-bearing for timers/alarms/reminders + the 3 system crons).
        "agent:\n"
        "  disabled_toolsets:\n"
        "    - browser\n"
        "    - code_execution\n"
        "    - image_gen\n"
        "    - video_gen\n"
        "    - delegation\n"
        "    - discord_admin\n"
        "    - x_search\n"
        "    - yuanbao\n"
        "    - moa\n"
        "    - computer_use\n"
        "    - kanban\n"
    )
    if custom_providers_block:
        content += "\n" + custom_providers_block
    if preserved_mcp_servers:
        sep = "" if preserved_mcp_servers.startswith("\n") else "\n"
        content += sep + preserved_mcp_servers
    try:
        with open(config_path, "w", encoding="utf-8") as f:
            f.write(content)
    except OSError as e:
        jlog(
            "error",
            "hermes:config",
            "could not write config.yaml",
            path=config_path,
            error=str(e),
        )
        return None
    # Make the dir traversable + the file readable so the later solbay/admin
    # phases (which read via the container) and any downstream merge can reach
    # it. Best-effort; a PermissionError here is non-fatal.
    try:
        os.chmod(config_dir, 0o755)
        os.chmod(config_path, 0o644)
    except OSError as e:
        jlog(
            "warn",
            "hermes:config",
            "could not relax config perms for downstream merges",
            path=config_dir,
            error=str(e),
        )
    jlog(
        "info",
        "hermes:config",
        "wrote config.yaml",
        path=config_path,
        model=model,
        provider_url=provider_url,
    )
    return config_path


# Hermes drops a stock SOUL.md on first boot whose first heading is this.
STOCK_SOUL_MARKER = "# Hermes Agent Persona"


def _soul_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _record_shipped_soul(marker_path: str, soul: str) -> None:
    """Write the sha256 of the just-installed shipped soul alongside it (#283),
    so a later redeploy can tell an unmodified shipped soul (safe to update)
    from an operator-edited one (must be preserved). Best-effort."""
    try:
        with open(marker_path, "w", encoding="utf-8") as f:
            f.write(_soul_sha256(soul) + "\n")
        os.chmod(marker_path, 0o644)
    except OSError as e:
        jlog(
            "warn",
            "hermes:soul",
            "could not record shipped-soul hash",
            path=marker_path,
            error=str(e),
        )


def write_soul_md(data_dir: str) -> bool:
    """Install the Solilos SOUL.md and keep it in sync with the shipped soul on
    redeploy without clobbering an operator-edited one (#283 sidecar-hash
    guard). Returns True when the file was written."""
    target = os.path.join(data_dir, "hermes", "SOUL.md")
    marker = os.path.join(data_dir, "hermes", ".soul.shipped.sha256")
    source = os.path.join(os.path.dirname(os.path.abspath(__file__)), "SOUL.md")
    try:
        with open(source, encoding="utf-8") as f:
            soul = f.read()
    except OSError as e:
        jlog("warn", "hermes:soul", "shipped SOUL.md missing — skipping", error=str(e))
        return False
    existing = ""
    if os.path.exists(target):
        try:
            with open(target, encoding="utf-8") as f:
                existing = f.read()
        except OSError:
            existing = ""
    if existing == soul:
        if not os.path.exists(marker):
            _record_shipped_soul(marker, soul)
        return False

    recorded = ""
    if os.path.exists(marker):
        try:
            with open(marker, encoding="utf-8") as f:
                recorded = f.read().strip()
        except OSError:
            recorded = ""

    if existing.strip():
        if recorded:
            if recorded != _soul_sha256(existing):
                jlog(
                    "info",
                    "hermes:soul",
                    "leaving operator-edited SOUL.md untouched — a shipped update is available",
                    path=target,
                )
                return False
        elif STOCK_SOUL_MARKER not in existing:
            jlog(
                "info",
                "hermes:soul",
                "leaving customised SOUL.md untouched",
                path=target,
            )
            return False
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as f:
            f.write(soul)
        os.chmod(target, 0o644)
    except OSError as e:
        jlog(
            "error", "hermes:soul", "could not write SOUL.md", path=target, error=str(e)
        )
        return False
    _record_shipped_soul(marker, soul)
    jlog("info", "hermes:soul", "installed Solilos SOUL.md", path=target)
    return True


def _provision_sb_mcp_token_once(sb_api: str, token_name: str) -> str | None:
    """One mint attempt against the canonical api-tokens route. Returns the
    `sb_`-shaped secret, or None on any failure."""
    status, body = post_json(
        f"{sb_api}/api/system/api-tokens",
        {"name": token_name, "scopes": ["read", "lifecycle"]},
        timeout=15,
    )
    if status != 200 or not isinstance(body, dict):
        return None
    secret = body.get("secret")
    if isinstance(secret, str) and SB_MCP_TOKEN_RE.match(secret):
        return secret
    return None


def provision_sb_mcp_token(sb_api: str, token_name: str = "solilos-mcp") -> str | None:
    """Mint a long-lived SB-MCP token (read+lifecycle) via the ServiceBay HTTP
    API. Retries a few times for the SB-on-loopback readiness race (#126).
    Returns the `sb_`-shaped secret, or None when every attempt failed — the
    caller must NOT persist a non-`sb_` fallback."""
    attempts = 4
    for attempt in range(1, attempts + 1):
        secret = _provision_sb_mcp_token_once(sb_api, token_name)
        if secret:
            jlog(
                "info",
                "hermes:sb-mcp",
                "minted SB-MCP token for Hermes auto-wiring",
                name=token_name,
                attempt=attempt,
            )
            return secret
        if attempt < attempts:
            time.sleep(3)
    jlog(
        "warn",
        "hermes:sb-mcp",
        "could not mint SB-MCP token via SB API after retries — leaving SB-MCP unwired (a missing entry is more diagnosable than a silently-401 one); mint from Settings → Integrations → MCP and add the entry by hand",
    )
    return None


def probe_sb_mcp_token(token: str) -> bool:
    """Live-validate a bearer against ServiceBay's `/mcp` with a JSON-RPC
    `initialize`. 200 = registered + accepted; 401 = stale/junk. A connection
    failure returns True so a transient loopback hiccup doesn't trigger a
    needless re-mint when the shape already passed (#126)."""
    if not token:
        return False
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "solilos-post-deploy", "version": "1"},
        },
    }
    req = urllib.request.Request(
        SB_MCP_URL,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
    )
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json, text/event-stream")
    req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return 200 <= resp.status < 300
    except urllib.error.HTTPError as e:
        return e.code != 401
    except (urllib.error.URLError, TimeoutError, OSError):
        return True


def _extract_servicebay_bearer(mcp_block: str) -> str | None:
    """Pull the `servicebay:` entry's bearer token out of an mcp_servers block
    string. Returns the token (any shape) or None if absent."""
    in_sb = False
    for line in mcp_block.splitlines():
        stripped = line.strip()
        if line[:2] == "  " and line[:3] != "   " and stripped.endswith(":"):
            in_sb = stripped == "servicebay:"
            continue
        if in_sb and stripped.startswith("Authorization:"):
            m = re.search(r"Bearer\s+(\S+)", stripped)
            return m.group(1).strip('"') if m else None
    return None


def ensure_sb_mcp_servers_block(config_path: str, sb_api: str) -> bool:
    """Ensure config.yaml carries a `mcp_servers.servicebay:` entry with a VALID
    bearer (#1045). Self-heal (#126): a present but invalid bearer is re-minted;
    a still-valid one is left untouched. Returns True when the file was mutated.

    Note: the later solbay phase rewrites the whole household `mcp_servers:`
    block (servicebay-mcp + gatekeeper-mcp) over the top of this in the same
    run, which is the intended end state — this entry seeds the block on a
    fresh install before solbay's merge runs."""
    if not os.path.exists(config_path):
        return False
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            existing = f.read()
    except OSError as e:
        jlog(
            "warn",
            "hermes:sb-mcp",
            "could not read config.yaml",
            path=config_path,
            error=str(e),
        )
        return False

    existing_mcp = _extract_top_level_block(existing, "mcp_servers")
    current_token = _extract_servicebay_bearer(existing_mcp)
    if current_token:
        if SB_MCP_TOKEN_RE.match(current_token) and probe_sb_mcp_token(current_token):
            return False
        jlog(
            "warn",
            "hermes:sb-mcp",
            "existing servicebay-mcp token is invalid (bad shape or 401) — re-minting",
        )

    secret = provision_sb_mcp_token(sb_api)
    if not secret:
        return False

    new_entry_lines = [
        "  servicebay:\n",
        f'    url: "{SB_MCP_URL}"\n',
        "    headers:\n",
        f'      Authorization: "Bearer {secret}"\n',
    ]

    if current_token is not None:
        healed_mcp = _replace_servicebay_entry(existing_mcp, new_entry_lines)
        new_content = existing.replace(existing_mcp, healed_mcp, 1)
    elif existing_mcp:
        appended = existing_mcp.rstrip("\n") + "\n" + "".join(new_entry_lines)
        new_content = existing.replace(existing_mcp, appended, 1)
    else:
        sep = "" if existing.endswith("\n\n") or existing.endswith("\n") else "\n"
        new_content = existing + sep + "mcp_servers:\n" + "".join(new_entry_lines)

    try:
        with open(config_path, "w", encoding="utf-8") as f:
            f.write(new_content)
    except OSError as e:
        jlog(
            "error",
            "hermes:sb-mcp",
            "could not write config.yaml after writing servicebay entry",
            path=config_path,
            error=str(e),
        )
        return False

    jlog(
        "info",
        "hermes:sb-mcp",
        "wrote mcp_servers.servicebay block — Hermes will reach SB-MCP on next start",
        path=config_path,
        url=SB_MCP_URL,
    )
    return True


def _replace_servicebay_entry(mcp_block: str, new_entry_lines: list[str]) -> str:
    """Swap the existing `servicebay:` sub-block for `new_entry_lines`,
    preserving every other entry."""
    lines = mcp_block.splitlines(keepends=True)
    out: list[str] = []
    in_sb = False
    for line in lines:
        is_entry_key = (
            line[:2] == "  " and line[:3] != "   " and line.strip().endswith(":")
        )
        if is_entry_key and line.strip() == "servicebay:":
            in_sb = True
            out.extend(new_entry_lines)
            continue
        if in_sb:
            if is_entry_key or (line.strip() and line[:1] not in (" ", "\t")):
                in_sb = False
                out.append(line)
            continue
        out.append(line)
    return "".join(out)


def write_gateway_env(data_dir: str, entries: dict[str, str]) -> bool:
    """Merge messaging-gateway credentials into `<DATA_DIR>/hermes/.env`. Merge
    semantics: overwrite managed keys, keep everything else; empty values clear
    a key. Returns True when the file changed."""
    config_dir = os.path.join(data_dir, "hermes")
    env_path = os.path.join(config_dir, ".env")
    managed_keys = {
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_ALLOWED_USERS",
        "DISCORD_BOT_TOKEN",
        "DISCORD_ALLOWED_CHANNELS",
        "SIGNAL_ACCOUNT",
        "SIGNAL_ALLOWED_USERS",
    }
    existing: dict[str, str] = {}
    order: list[str] = []
    preamble: list[str] = []
    if os.path.exists(env_path):
        try:
            with open(env_path, encoding="utf-8") as f:
                for raw in f:
                    line = raw.rstrip("\n")
                    if not line.strip() or line.lstrip().startswith("#"):
                        preamble.append(line)
                        continue
                    if "=" not in line:
                        preamble.append(line)
                        continue
                    key, _, value = line.partition("=")
                    key = key.strip()
                    if not key:
                        preamble.append(line)
                        continue
                    if key in managed_keys:
                        continue
                    existing[key] = value
                    order.append(key)
        except OSError as e:
            jlog(
                "warn",
                "hermes:env",
                "could not read .env, will recreate",
                path=env_path,
                error=str(e),
            )
            existing = {}
            order = []
            preamble = []

    new_managed = {k: v for k, v in entries.items() if v}
    desired_lines: list[str] = []
    for line in preamble:
        desired_lines.append(line)
    for key in order:
        desired_lines.append(f"{key}={existing[key]}")
    if new_managed:
        if desired_lines and desired_lines[-1] != "":
            desired_lines.append("")
        for key in sorted(new_managed):
            desired_lines.append(f"{key}={new_managed[key]}")
    new_content = "\n".join(desired_lines).rstrip("\n") + (
        "\n" if desired_lines else ""
    )

    if not os.path.exists(env_path) and not new_managed:
        return False

    try:
        with open(env_path, encoding="utf-8") as f:
            old_content = f.read()
    except (FileNotFoundError, OSError):
        old_content = ""
    if old_content == new_content:
        return False

    try:
        os.makedirs(config_dir, exist_ok=True)
        with open(env_path, "w", encoding="utf-8") as f:
            f.write(new_content)
        os.chmod(env_path, 0o600)
    except OSError as e:
        jlog("error", "hermes:env", "could not write .env", path=env_path, error=str(e))
        return False
    jlog(
        "info",
        "hermes:env",
        "updated messaging-gateway .env",
        path=env_path,
        keys=sorted(new_managed.keys()),
    )
    return True


def write_ddgs_install_script(data_dir: str) -> bool:
    """Write the startup script to <DATA_DIR>/hermes/99-install-ddgs (mounted at
    /etc/cont-init.d/99-install-ddgs) so the ddgs package installs on container
    boot. Returns True when the file was written."""
    target = os.path.join(data_dir, "hermes", "99-install-ddgs")
    script = (
        "#!/bin/sh\n"
        'if ! /opt/hermes/.venv/bin/python -c "import ddgs" >/dev/null 2>&1; then\n'
        "    echo 'Installing ddgs library for DuckDuckGo Search...'\n"
        "    /opt/hermes/bin/hermes tools post-setup ddgs || /opt/hermes/.venv/bin/pip install ddgs || true\n"
        "fi\n"
    )
    if os.path.exists(target):
        try:
            with open(target, "r", encoding="utf-8") as f:
                if f.read() == script:
                    return False
        except OSError:
            pass
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as f:
            f.write(script)
        os.chmod(target, 0o755)
        jlog("info", "hermes:ddgs-script", "wrote ddgs install script", path=target)
        return True
    except OSError as e:
        jlog(
            "error",
            "hermes:ddgs-script",
            "could not write ddgs install script",
            path=target,
            error=str(e),
        )
        return False


# A graceful pod shutdown records the admin gateway's gateway_state as `stopped`,
# so the image's 02-reconcile-profiles reconciler (which only auto-starts profiles
# whose recorded state is `running`) would leave admin DOWN after a plain restart
# or reboot — only a ServiceBay deploy (which re-runs start_admin_gateway) brought
# it back (#299). This cont-init hook runs BEFORE 02-reconcile (it sorts `016-` <
# `02-`) and forces admin's recorded state to `running`, so the reconciler
# auto-starts the admin gateway on EVERY boot. It writes as the hermes user
# (mirroring 02-reconcile's uid drop) so the state file stays hermes-owned and the
# running gateway can keep updating it. No-op until the admin profile exists.
ADMIN_GATEWAY_BOOT_HOOK = "016-ensure-admin-gateway"
_ADMIN_GATEWAY_BOOT_HOOK_SCRIPT = (
    "#!/command/with-contenv sh\n"
    "# Keep the admin profile gateway up across reboots (#299): force its recorded\n"
    "# gateway_state to `running` before 02-reconcile-profiles auto-starts it.\n"
    'state="/opt/data/profiles/admin/gateway_state.json"\n'
    '[ -f "$state" ] || exit 0\n'
    'run() { [ "$(id -u)" = 0 ] && set -- s6-setuidgid hermes "$@"; "$@"; }\n'
    "run /opt/hermes/.venv/bin/python - \"$state\" <<'PY' || true\n"
    "import json, sys\n"
    "p = sys.argv[1]\n"
    "try:\n"
    "    d = json.load(open(p))\n"
    "except Exception:\n"
    "    d = {}\n"
    'd["gateway_state"] = "running"\n'
    'json.dump(d, open(p, "w"))\n'
    "PY\n"
)


def write_admin_gateway_boot_hook() -> bool:
    """Write the cont-init hook (`/opt/data/016-ensure-admin-gateway`, mounted at
    /etc/cont-init.d/016-ensure-admin-gateway) that keeps the admin gateway up
    across reboots (#299).

    Written + chmod'd VIA THE CONTAINER: `/opt/data` is hermes-owned mode 0700, so
    the post-deploy's host-side write silently fails and the subPath mount source
    goes missing → the hermes container can't start at the next restart (the pod
    goes Degraded; box-verified the hard way). MUST run after wait_for_hermes (the
    container must be up to exec) and BEFORE the final restart (so the file is on
    the volume when the pod is recreated with the mount). Returns True on write."""
    target = f"/opt/data/{ADMIN_GATEWAY_BOOT_HOOK}"
    if read_file_in_container(target) == _ADMIN_GATEWAY_BOOT_HOOK_SCRIPT:
        return False
    if not write_file_in_container(target, _ADMIN_GATEWAY_BOOT_HOOK_SCRIPT):
        return False
    # cont-init.d scripts must be executable.
    try:
        subprocess.run(
            ["podman", "exec", HERMES_CONTAINER, "chmod", "0755", target],
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        pass
    jlog(
        "info",
        "hermes:admin-boot-hook",
        "wrote admin-gateway boot hook (#299)",
        path=target,
    )
    return True


def _ha_token_timeout() -> int:
    return int(os.environ.get("HA_TOKEN_TIMEOUT", "90"))


def _ha_api_timeout() -> int:
    return int(os.environ.get("HA_API_TIMEOUT", "60"))


def _wait_for_ha_token(token_path: str, deadline_secs: int | None = None) -> str | None:
    """#1002 — Poll for the HA long-lived token file HA's post-deploy writes
    near the end of its run. Returns the token once present + non-empty, or None
    at the deadline (0 = check once)."""
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
    """#1002 — Probe HA's /api/ with the new token until it answers 200, so the
    first Hermes HA-gateway reconnect doesn't land during HA's startup window.
    Best-effort (0 = skip the probe)."""
    if timeout_secs is None:
        timeout_secs = _ha_api_timeout()
    if timeout_secs <= 0:
        return False
    deadline = time.time() + timeout_secs
    last_status = 0
    while time.time() < deadline:
        try:
            req = urllib.request.Request(
                "http://127.0.0.1:8123/api/",
                headers={"Authorization": f"Bearer {token}"},
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                last_status = resp.status
                if 200 <= resp.status < 300:
                    return True
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError):
            pass
        time.sleep(3)
    jlog(
        "warn",
        "hermes:ha-ready",
        "HA /api/ not 200 within deadline; restart anyway",
        last_status=last_status,
        deadline_secs=timeout_secs,
    )
    return False


def adopt_ha_long_lived_token(data_dir: str) -> str | None:
    """Pick up HA's auto-onboarded long-lived token (#934/#1002) and patch the
    deployed `solilos.yml` pod manifest's HASS_TOKEN env value so Hermes' native
    HA gateway can authenticate. Returns the token, or None when the file never
    appears or the patch is a no-op."""
    token_path = os.path.join(
        data_dir, "home-assistant", "homeassistant", ".solilos-long-lived-token"
    )
    token = _wait_for_ha_token(token_path)
    if token is None:
        jlog(
            "info",
            "hermes:ha-token",
            "no HA long-lived token after retry — likely operator opted out of HA auto-onboarding",
            path=token_path,
        )
        return None
    # The merged pod yml is written by ServiceBay's install runner to the
    # user-Quadlet dir as `solilos.yml` (the Pod is named `solilos`). Patch the
    # HASS_TOKEN env value in-place so a subsequent restart picks up the real
    # token.
    pod_yml = os.path.expanduser("~/.config/containers/systemd/solilos.yml")
    if not os.path.exists(pod_yml):
        jlog(
            "warn",
            "hermes:ha-token",
            "solilos.yml not found at expected path",
            path=pod_yml,
        )
        return None
    try:
        with open(pod_yml, encoding="utf-8") as f:
            src = f.read()
    except OSError as e:
        jlog(
            "warn",
            "hermes:ha-token",
            "could not read solilos.yml",
            path=pod_yml,
            error=str(e),
        )
        return None
    new = re.sub(
        r"(- name: HASS_TOKEN\n\s+value: )[^\n]+",
        lambda m: m.group(1) + '"' + token + '"',
        src,
    )
    if new == src:
        return token
    try:
        with open(pod_yml, "w", encoding="utf-8") as f:
            f.write(new)
    except OSError as e:
        jlog(
            "warn",
            "hermes:ha-token",
            "could not write patched solilos.yml",
            path=pod_yml,
            error=str(e),
        )
        return None
    jlog(
        "info",
        "hermes:ha-token",
        "adopted HA long-lived token from home-assistant post-deploy",
        token_path=token_path,
    )
    _wait_for_ha_api(token)
    return token


def _ha_get(path: str, token: str, timeout: float = 10.0) -> tuple[int, object]:
    """GET against HA's API with the long-lived token. 0 on connection failure."""
    req = urllib.request.Request(
        f"http://127.0.0.1:8123{path}",
        headers={"Authorization": f"Bearer {token}"},
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
    """POST JSON against HA's API with the long-lived token. 0 on connection
    failure."""
    req = urllib.request.Request(
        f"http://127.0.0.1:8123{path}",
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


def ensure_ha_jellyfin_integration(
    token: str, url: str, username: str, password: str
) -> bool:
    """Auto-install HA's `jellyfin` integration via HA's config-entries flow API
    (#195) so `media_player.jellyfin_*` entities appear. Idempotent (skips if an
    entry exists) + fail-soft. Returns True only when a new entry was created."""
    if not (token and url and username):
        return False

    status, entries = _ha_get("/api/config/config_entries/entry", token)
    if status != 200 or not isinstance(entries, list):
        jlog(
            "warn",
            "hermes:jellyfin",
            "could not list HA config entries — skipping Jellyfin auto-install",
            status=status,
        )
        return False
    if any(isinstance(e, dict) and e.get("domain") == "jellyfin" for e in entries):
        jlog(
            "info",
            "hermes:jellyfin",
            "HA jellyfin config entry already present — nothing to do",
        )
        return False

    status, flow = _ha_post(
        "/api/config/config_entries/flow", token, {"handler": "jellyfin"}
    )
    if status != 200 or not isinstance(flow, dict) or not flow.get("flow_id"):
        jlog(
            "warn",
            "hermes:jellyfin",
            "could not start HA jellyfin config flow — skipping auto-install",
            status=status,
        )
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
        jlog(
            "info",
            "hermes:jellyfin",
            "created HA jellyfin config entry — media_player.jellyfin_* entities will appear",
            url=url,
        )
        return True

    errors = result.get("errors") if isinstance(result, dict) else None
    _ha_request_delete(f"/api/config/config_entries/flow/{flow_id}", token)
    jlog(
        "warn",
        "hermes:jellyfin",
        "HA jellyfin config flow did not create an entry — check JELLYFIN_* and that Jellyfin is reachable",
        status=status,
        errors=errors,
    )
    return False


def _ha_request_delete(path: str, token: str, timeout: float = 10.0) -> None:
    """Best-effort DELETE against HA's API (used to abort a dangling flow)."""
    req = urllib.request.Request(
        f"http://127.0.0.1:8123{path}",
        headers={"Authorization": f"Bearer {token}"},
        method="DELETE",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout):
            return
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError):
        return


# ════════════════════════════════════════════════════════════════════════════
# 2. CHAT PHASE — decommission retired chat pods (#139/#140). Was
#    templates/solilos-chat/post-deploy.py.
# ════════════════════════════════════════════════════════════════════════════

# Ordered oldest → newest so an archive of open-webui lands before any
# hermes-webui teardown that shares no data.
RETIRED_NAMES = ("open-webui", "hermes-webui")


def http_request(
    url: str,
    method: str = "GET",
    payload: dict[str, object] | None = None,
    timeout: float = 15.0,
) -> tuple[int, object | None]:
    headers = {"Content-Type": "application/json"}
    token = os.environ.get("SB_API_TOKEN", "")
    if token:
        headers["X-SB-Internal-Token"] = token
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            try:
                return resp.status, json.loads(body) if body else None
            except json.JSONDecodeError:
                return resp.status, None
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8"))
        except Exception:  # pylint: disable=broad-except
            return e.code, None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        jlog("warn", "solilos:decom", "HTTP error", url=url, error=str(e))
        return 0, None


def get_installed_templates(sb_api: str) -> dict[str, object] | None:
    status, body = http_request(f"{sb_api}/api/settings")
    if status != 200 or not isinstance(body, dict):
        return None
    installed = body.get("installedTemplates")
    return installed if isinstance(installed, dict) else None


def archive_data_dir(data_dir: str, name: str) -> str | None:
    src = os.path.join(data_dir, name)
    if not os.path.isdir(src):
        return None
    stamp = datetime.datetime.now().strftime("%Y-%m-%d-%H%M%S")
    archive_root = os.path.join(data_dir, "_archived")
    dst = os.path.join(archive_root, f"{name}-{stamp}")
    try:
        os.makedirs(archive_root, exist_ok=True)
        os.rename(src, dst)
    except OSError as e:
        jlog(
            "warn",
            "solilos:decom",
            "could not archive data dir; left in place for manual cleanup",
            src=src,
            error=str(e),
        )
        return None
    jlog("info", "solilos:decom", "archived data dir", src=src, dst=dst)
    return dst


def delete_service(sb_api: str, name: str) -> bool:
    status, _ = http_request(
        f"{sb_api}/api/services/{name}",
        method="DELETE",
        timeout=30,
    )
    if status == 200:
        jlog("info", "solilos:decom", "deleted service via SB API", service=name)
        return True
    jlog(
        "warn",
        "solilos:decom",
        "could not delete service via SB API — operator may need to remove the pod manually",
        service=name,
        status=status,
    )
    return False


def remove_from_installed_templates(
    sb_api: str, installed: dict[str, object], names: list[str]
) -> None:
    to_prune = [n for n in names if n in installed]
    if not to_prune:
        return
    pruned = {k: v for k, v in installed.items() if k not in to_prune}
    status, _ = http_request(
        f"{sb_api}/api/settings",
        method="POST",
        payload={"installedTemplates": pruned},
        timeout=15,
    )
    if status == 200:
        jlog(
            "info",
            "solilos:decom",
            "removed retired templates from installedTemplates",
            removed=to_prune,
        )
        return
    jlog(
        "warn",
        "solilos:decom",
        "could not update installedTemplates — SB will keep showing them as installed until the next config edit",
        status=status,
    )


def decommission(sb_api: str, data_dir: str) -> None:
    installed = get_installed_templates(sb_api)
    if installed is None:
        jlog(
            "warn",
            "solilos:decom",
            "could not read installedTemplates; skipping decommission check",
        )
        return
    present = [name for name in RETIRED_NAMES if name in installed]
    if not present:
        return  # Fresh install or already-decommissioned — no-op
    jlog(
        "info",
        "solilos:decom",
        "retired chat pods detected — beginning decommission for #139/#140",
        present=present,
    )
    for name in present:
        archive_data_dir(data_dir, name)
        delete_service(sb_api, name)
    remove_from_installed_templates(sb_api, installed, present)
    jlog("info", "solilos:decom", "decommission complete", removed=present)


# ════════════════════════════════════════════════════════════════════════════
# 3. SOLBAY PHASE — wait_for_hermes, register crons, collect + merge the
#    household mcp_servers block (via the solilos-hermes container). Was
#    templates/solbay/post-deploy.py.
# ════════════════════════════════════════════════════════════════════════════

# Set in main() from env.
SB_API_URL = "http://127.0.0.1:3000"
HERMES_API_PORT = "8642"
HERMES_API_KEY = ""
HERMES_API_URL = "http://127.0.0.1:8642"
SERVICEBAY_MCP_URL = ""
GATEKEEPER_MCP_URL = ""
GATEKEEPER_MCP_TOKEN = ""
READINESS_TIMEOUT_S = 120


def hermes_get(path: str, timeout: float = 5.0) -> int:
    """GET against Hermes' API with bearer auth. 0 for connection failure."""
    req = urllib.request.Request(f"{HERMES_API_URL}{path}", method="GET")
    if HERMES_API_KEY:
        req.add_header("Authorization", f"Bearer {HERMES_API_KEY}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code
    except (urllib.error.URLError, ConnectionError, TimeoutError, OSError):
        return 0


def hermes_request_json(
    path: str,
    method: str = "GET",
    payload: dict[str, object] | None = None,
    timeout: float = 10.0,
) -> tuple[int, object | None]:
    """Call Hermes' API with bearer auth, returning (status, parsed-body)."""
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(f"{HERMES_API_URL}{path}", data=data, method=method)
    req.add_header("Authorization", f"Bearer {HERMES_API_KEY}")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            try:
                return resp.status, json.loads(raw) if raw else None
            except json.JSONDecodeError:
                return resp.status, None
    except urllib.error.HTTPError as e:
        return e.code, None
    except (urllib.error.URLError, ConnectionError, TimeoutError, OSError):
        return 0, None


def _mint_servicebay_mcp_token_once() -> str | None:
    """One mint attempt for the household servicebay-mcp token. Returns the
    `sb_`-shaped secret, or None on any failure."""
    status, body = post_json(
        f"{SB_API_URL}/api/system/api-tokens",
        {"name": "solbay-hermes", "scopes": ["read", "mutate", "lifecycle"]},
        timeout=15,
    )
    if status != 200 or not isinstance(body, dict):
        return None
    secret = body.get("secret")
    if not isinstance(secret, str) or not SB_MCP_TOKEN_RE.match(secret):
        return None
    return secret


def mint_servicebay_mcp_token(attempts: int = 4, backoff_s: float = 3.0) -> str | None:
    """Mint a real household servicebay-mcp token. Retries for the SB readiness
    race (#126); never persists a non-`sb_` fallback."""
    for attempt in range(1, attempts + 1):
        secret = _mint_servicebay_mcp_token_once()
        if secret:
            jlog("info", "solbay:mcp", "minted servicebay-mcp token", attempt=attempt)
            return secret
        if attempt < attempts:
            time.sleep(backoff_s)
    jlog(
        "warn",
        "solbay:mcp",
        "could not mint servicebay-mcp token after retries; skipping servicebay-mcp entry (a missing entry is more diagnosable than a silently-401 one)",
        attempts=attempts,
    )
    return None


def probe_servicebay_mcp_token(token: str) -> bool:
    """Live-validate a household servicebay-mcp bearer against `/mcp`. 200 = ok;
    401 = stale. Connection failure returns True (shape already passed)."""
    if not token or not SERVICEBAY_MCP_URL:
        return False
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "solbay-post-deploy", "version": "1"},
        },
    }
    req = urllib.request.Request(
        SERVICEBAY_MCP_URL,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
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


def read_config_via_container() -> str | None:
    """Read /opt/data/config.yaml from inside the hermes container (its user
    owns the file, mode 640). Returns the content, or None when absent / the
    exec fails."""
    try:
        proc = subprocess.run(
            ["podman", "exec", HERMES_CONTAINER, "cat", CONTAINER_CONFIG_PATH],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as e:
        jlog(
            "warn",
            "solbay:config",
            "could not exec into hermes container to read config.yaml",
            container=HERMES_CONTAINER,
            error=str(e),
        )
        return None
    if proc.returncode != 0:
        jlog(
            "warn",
            "solbay:config",
            "config.yaml not found",
            path=f"{HERMES_CONTAINER}:{CONTAINER_CONFIG_PATH}",
            stderr=proc.stderr.strip(),
        )
        return None
    return proc.stdout


def write_config_via_container(content: str) -> bool:
    """Write config.yaml inside the hermes container via `podman exec -i` so it
    lands owned by the hermes user. Returns True on success."""
    try:
        proc = subprocess.run(
            [
                "podman",
                "exec",
                "-i",
                HERMES_CONTAINER,
                "sh",
                "-c",
                f"cat > {CONTAINER_CONFIG_PATH}",
            ],
            input=content,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as e:
        jlog(
            "error",
            "solbay:config",
            "could not exec into hermes container to write config.yaml",
            container=HERMES_CONTAINER,
            error=str(e),
        )
        return False
    if proc.returncode != 0:
        jlog(
            "error",
            "solbay:config",
            "writing config.yaml in hermes container failed",
            container=HERMES_CONTAINER,
            stderr=proc.stderr.strip(),
        )
        return False
    return True


def strip_mcp_servers_block(content: str) -> str:
    """Remove an existing top-level `mcp_servers:` block (column-0 key → next
    top-level key / EOF). Idempotent when absent."""
    lines = content.splitlines(keepends=True)
    out: list[str] = []
    in_block = False
    for line in lines:
        if not in_block:
            stripped = line.lstrip()
            if line[:1] not in (" ", "\t") and stripped.startswith("mcp_servers:"):
                in_block = True
                continue
            out.append(line)
        else:
            stripped = line.lstrip()
            if (
                line[:1] not in (" ", "\t")
                and stripped
                and not stripped.startswith("#")
            ):
                in_block = False
                out.append(line)
    return "".join(out)


def ensure_supports_vision(content: str) -> str:
    """Inject `supports_vision: true` under the top-level `model:` mapping so
    Hermes natively attaches inbound images on a local Ollama model (#202).
    Idempotent."""
    if "supports_vision" in content:
        return content
    lines = content.splitlines(keepends=True)
    out: list[str] = []
    injected = False
    for line in lines:
        out.append(line)
        if not injected and line.rstrip("\n").rstrip() == "model:":
            out.append("  supports_vision: true\n")
            injected = True
    return "".join(out) if injected else content


def existing_servicebay_mcp_token() -> str | None:
    """Pull the current `servicebay-mcp` bearer from the live config.yaml.
    Returns it only when present AND `sb_`-shaped (junk reads as None)."""
    content = read_config_via_container()
    if not content:
        return None
    in_sb = False
    for line in content.splitlines():
        stripped = line.strip()
        if line[:1] not in (" ", "\t"):
            in_sb = False
        if stripped.startswith("servicebay-mcp:"):
            in_sb = True
            continue
        if in_sb and stripped.startswith("Authorization:"):
            m = re.search(r"Bearer\s+(\S+)", stripped)
            token = m.group(1).strip('"') if m else ""
            return token if SB_MCP_TOKEN_RE.match(token) else None
    return None


def render_mcp_block(servers: list[tuple[str, str, str]]) -> str:
    """Render an `mcp_servers:` block for (name, url, token) entries. A
    token-less server gets NO headers (an empty `Authorization: "Bearer "` makes
    Hermes reject it)."""
    if not servers:
        return ""
    parts: list[str] = ["mcp_servers:\n"]
    for name, url, token in servers:
        parts.append(f"  {name}:\n")
        parts.append(f'    url: "{url}"\n')
        if token:
            parts.append("    headers:\n")
            parts.append(f'      Authorization: "Bearer {token}"\n')
    return "".join(parts)


def merge_config_yaml(servers: list[tuple[str, str, str]]) -> bool:
    """Read config.yaml (via the container), strip any existing mcp_servers
    block, append the rendered household one. Returns True on write.

    The rewrite intentionally re-renders ONLY the household servers
    (servicebay-mcp + gatekeeper-mcp) and so DROPS the operator
    `servicebay_admin` entry from the shared config — a ~6.3k-token near-dup
    that only the operator soul needs and that would bloat every household
    chat's prefill (#268). The admin phase (step 4) re-splices servicebay_admin
    right after this in the same run, so the operator wiring stays intact while
    a later household-only redeploy keeps it out of the household-facing config."""
    existing = read_config_via_container()
    if existing is None:
        return False
    stripped = ensure_supports_vision(strip_mcp_servers_block(existing))
    block = render_mcp_block(servers)
    if stripped and not stripped.endswith("\n"):
        stripped += "\n"
    merged = stripped + ("\n" + block if block else "")
    if not write_config_via_container(merged):
        return False
    jlog(
        "info",
        "solbay:config",
        "config.yaml mcp_servers block updated",
        path=f"{HERMES_CONTAINER}:{CONTAINER_CONFIG_PATH}",
        mcp_servers=[name for name, _, _ in servers],
    )
    return True


def wait_for_hermes() -> None:
    deadline = time.time() + READINESS_TIMEOUT_S
    last_status: int | None = None
    while time.time() < deadline:
        status = hermes_get("/health")
        if status in (200, 401, 403):
            jlog("info", "solbay:hermes", "ready", status=status)
            return
        last_status = status
        time.sleep(2)
    jlog(
        "error",
        "solbay:hermes",
        "not reachable within readiness window",
        last_status=last_status,
        timeout_s=READINESS_TIMEOUT_S,
    )
    raise SystemExit(1)


def collect_mcp_servers() -> list[tuple[str, str, str]]:
    """Pair each household MCP with its token; skip empty entries. (ha-mcp is
    intentionally not wired — HA is served by Hermes' native homeassistant
    toolset.)"""
    servers: list[tuple[str, str, str]] = []
    if SERVICEBAY_MCP_URL:
        current = existing_servicebay_mcp_token()
        if current and probe_servicebay_mcp_token(current):
            jlog(
                "info",
                "solbay:mcp",
                "servicebay-mcp token still valid; keeping existing",
            )
            servers.append(("servicebay-mcp", SERVICEBAY_MCP_URL, current))
        else:
            token = mint_servicebay_mcp_token()
            if token:
                servers.append(("servicebay-mcp", SERVICEBAY_MCP_URL, token))
            else:
                jlog(
                    "warn",
                    "solbay:mcp",
                    "servicebay-mcp skipped",
                    reason="no valid existing token and mint failed (will retry on next redeploy)",
                )
    else:
        jlog("info", "solbay:mcp", "servicebay-mcp skipped", reason="missing url")
    if GATEKEEPER_MCP_URL:
        servers.append(("gatekeeper-mcp", GATEKEEPER_MCP_URL, GATEKEEPER_MCP_TOKEN))
    else:
        jlog("info", "solbay:mcp", "gatekeeper-mcp skipped", reason="missing url")
    return servers


CHRONICLE_JOB_NAME = "sol-daily-chronicle"
PROBLEM_SUMMARIZER_JOB_NAME = "sol-problem-summarizer"
CHAT_COMPACTOR_JOB_NAME = "sol-chat-compactor"


def _register_cron(name: str, schedule: str, prompt: str) -> None:
    """Register a Hermes cron job by name, idempotently (skips when one of the
    same name already exists). Over HTTP so jobs.json stays hermes-owned."""
    status, body = hermes_request_json("/api/jobs", "GET")
    if status == 0:
        jlog(
            "warn",
            "solbay:cron",
            "cron skipped — Hermes jobs API unreachable",
            name=name,
        )
        return
    jobs = body if isinstance(body, list) else (body or {}).get("jobs", [])
    if any(isinstance(j, dict) and j.get("name") == name for j in jobs):
        jlog("info", "solbay:cron", "cron already present", name=name)
        return
    payload = {
        "name": name,
        "schedule": schedule,
        "prompt": prompt,
        "skills": [name],
        "deliver": "local",
    }
    create_status, _ = hermes_request_json("/api/jobs", "POST", payload)
    if create_status in (200, 201):
        jlog("info", "solbay:cron", "registered cron", name=name, schedule=schedule)
    else:
        jlog(
            "warn",
            "solbay:cron",
            "cron registration failed",
            name=name,
            status=create_status,
        )


def register_chronicle_cron() -> None:
    """Register the daily family-chronicle cron job (#83)."""
    _register_cron(
        CHRONICLE_JOB_NAME,
        "59 23 * * *",
        "Write today's family chronicle / journal entry for today. "
        "This is the unattended daily run — no resident is present, so "
        "do not ask anyone for highlights; compile from the day's "
        "ingested notes and household events you can see, and write a "
        "short honest entry (or skip a section) rather than inventing.",
    )


def register_problem_summarizer_cron() -> None:
    """Register the weekly troubleshooting-KB cron job (#182)."""
    _register_cron(
        PROBLEM_SUMMARIZER_JOB_NAME,
        "30 4 * * 1",
        "Update the troubleshooting knowledge base. This is the unattended "
        "weekly run — no admin is present, so do not ask anyone for input. "
        "Inspect recent system logs and past diagnostic conversations, "
        "extract resolved problem→indicators→solution sequences, and merge "
        "them into /opt/data/notes/knowledge-base/troubleshooting.md "
        "(append new problems, update existing ones in place). If nothing "
        "new surfaced, leave the file untouched rather than inventing.",
    )


def register_chat_compactor_cron() -> None:
    """Register the nightly chat-compaction cron job (#210)."""
    _register_cron(
        CHAT_COMPACTOR_JOB_NAME,
        "15 4 * * *",
        "Compact stale, long chat sessions. This is the unattended nightly run "
        "— no one is present, so do not ask for input. For each stale long "
        "conversation: FIRST extract its durable learnings (facts, decisions, "
        "household preferences, people, routines) into your memory with "
        "fact_store — but SKIP pure device-control, tool-call, or trivial "
        "confirmation turns, and do NOT memorise device/room/entity mappings or "
        "device state (those live in Home Assistant). THEN summarize the "
        "transcript so the chat can continue in a small context. Never delete a "
        "chat; the original transcript stays. If nothing is stale enough to "
        "compact, do nothing.",
    )


# ════════════════════════════════════════════════════════════════════════════
# 4. ADMIN-SOUL PHASE — splice the operator `servicebay_admin` (read+lifecycle+
#    mutate) mcp entry, leaving the household entries untouched. Was
#    templates/admin-soul/post-deploy.py.
# ════════════════════════════════════════════════════════════════════════════

ADMIN_MCP_NAME = "servicebay_admin"
ADMIN_MCP_SCOPES = ["read", "lifecycle", "mutate"]
ADMIN_TOKEN_NAME = "admin-soul"


def _mint_admin_token_once() -> str | None:
    """One mint attempt for the full-admin token."""
    status, body = post_json(
        f"{SB_API_URL}/api/system/api-tokens",
        {"name": ADMIN_TOKEN_NAME, "scopes": ADMIN_MCP_SCOPES},
        timeout=15,
    )
    if status != 200 or not isinstance(body, dict):
        return None
    secret = body.get("secret")
    if isinstance(secret, str) and SB_MCP_TOKEN_RE.match(secret):
        return secret
    return None


def mint_admin_token(attempts: int = 4, backoff_s: float = 3.0) -> str | None:
    """Mint a read+lifecycle+mutate ServiceBay-MCP token for the operator soul.
    Retries for the SB readiness race (#126); never persists a non-`sb_`
    fallback."""
    for attempt in range(1, attempts + 1):
        secret = _mint_admin_token_once()
        if secret:
            jlog(
                "info",
                "admin-soul:mcp",
                "minted admin SB-MCP token",
                scopes=ADMIN_MCP_SCOPES,
                attempt=attempt,
            )
            return secret
        if attempt < attempts:
            time.sleep(backoff_s)
    jlog(
        "warn",
        "admin-soul:mcp",
        "could not mint admin SB-MCP token after retries; skipping servicebay_admin entry (a missing entry is more diagnosable than a silently-401 one)",
        attempts=attempts,
    )
    return None


def probe_admin_token(token: str) -> bool:
    """Live-validate an admin bearer against `/mcp`. 200 = ok; 401 = stale.
    Connection failure returns True."""
    if not token or not SERVICEBAY_MCP_URL:
        return False
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "admin-soul-post-deploy", "version": "1"},
        },
    }
    req = urllib.request.Request(
        SERVICEBAY_MCP_URL,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
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


def extract_mcp_block(content: str) -> str:
    """Return the top-level `mcp_servers:` block (header + indented body), or
    '' if absent."""
    out: list[str] = []
    in_block = False
    for line in content.splitlines(keepends=True):
        stripped = line.lstrip()
        if not in_block:
            if line[:1] not in (" ", "\t") and stripped.startswith("mcp_servers:"):
                in_block = True
                out.append(line)
            continue
        if line.strip() == "" or line[:1] in (" ", "\t"):
            out.append(line)
        else:
            break
    return "".join(out)


def extract_admin_token(mcp_block: str) -> str | None:
    """Pull the `servicebay_admin` entry's bearer from an mcp_servers block."""
    in_entry = False
    for line in mcp_block.splitlines():
        stripped = line.strip()
        if line[:2] == "  " and line[:3] != "   " and stripped.endswith(":"):
            in_entry = stripped == f"{ADMIN_MCP_NAME}:"
            continue
        if in_entry and stripped.startswith("Authorization:"):
            m = re.search(r"Bearer\s+(\S+)", stripped)
            return m.group(1).strip('"') if m else None
    return None


def _admin_entry_lines(secret: str) -> list[str]:
    return [
        f"  {ADMIN_MCP_NAME}:\n",
        f'    url: "{SERVICEBAY_MCP_URL}"\n',
        "    headers:\n",
        f'      Authorization: "Bearer {secret}"\n',
    ]


def _replace_admin_entry(mcp_block: str, new_entry_lines: list[str]) -> str:
    """Swap the existing `servicebay_admin` sub-block for `new_entry_lines`,
    preserving every other entry."""
    out: list[str] = []
    in_entry = False
    for line in mcp_block.splitlines(keepends=True):
        is_entry_key = (
            line[:2] == "  " and line[:3] != "   " and line.strip().endswith(":")
        )
        if is_entry_key and line.strip() == f"{ADMIN_MCP_NAME}:":
            in_entry = True
            out.extend(new_entry_lines)
            continue
        if in_entry:
            if is_entry_key or (line.strip() and line[:1] not in (" ", "\t")):
                in_entry = False
                out.append(line)
            continue
        out.append(line)
    return "".join(out)


def ensure_admin_mcp_entry() -> bool:
    """Ensure config.yaml carries a `mcp_servers.servicebay_admin` entry with a
    VALID full-admin bearer, leaving every other MCP entry untouched (#175).
    Self-heal (#126); never persists a non-`sb_` token. Returns True on mutate."""
    existing = read_config_via_container()
    if existing is None:
        return False

    existing_mcp = extract_mcp_block(existing)
    current = extract_admin_token(existing_mcp)
    if current:
        if SB_MCP_TOKEN_RE.match(current) and probe_admin_token(current):
            jlog(
                "info",
                "admin-soul:mcp",
                "servicebay_admin token still valid; keeping existing",
            )
            return False
        jlog(
            "warn",
            "admin-soul:mcp",
            "existing servicebay_admin token is invalid (bad shape or 401) — re-minting",
        )

    secret = mint_admin_token()
    if not secret:
        return False

    entry_lines = _admin_entry_lines(secret)
    if current is not None:
        healed = _replace_admin_entry(existing_mcp, entry_lines)
        new_content = existing.replace(existing_mcp, healed, 1)
    elif existing_mcp:
        appended = existing_mcp.rstrip("\n") + "\n" + "".join(entry_lines)
        new_content = existing.replace(existing_mcp, appended, 1)
    else:
        sep = "" if existing.endswith("\n") else "\n"
        new_content = existing + sep + "mcp_servers:\n" + "".join(entry_lines)

    if not write_config_via_container(new_content):
        return False
    jlog(
        "info",
        "admin-soul:mcp",
        "wrote mcp_servers.servicebay_admin entry — operator soul gains full-admin SB-MCP on next Hermes start",
        path=f"{HERMES_CONTAINER}:{CONTAINER_CONFIG_PATH}",
        scopes=ADMIN_MCP_SCOPES,
    )
    return True


# ════════════════════════════════════════════════════════════════════════════
# 4b. PROFILE PROVISIONING — the multi-profile Hermes foundation (#293a).
#
# Instead of one global config.yaml shared by every session, provision two
# Hermes profiles and configure each. A profile lives under
# /opt/data/profiles/<name> (host <DATA_DIR>/hermes/profiles/<name>, inside the
# hermes-data volume) and carries its own config.yaml + SOUL.md + skills +
# `.no-bundled-skills` marker. The per-profile gateway containers + routing land
# in #293b/c; this step only provisions the profile *configs* so the foundation
# is in place and box-verifiable.
#
# Box-confirmed recipe baked in per profile:
#   - model.provider: ollama + providers.ollama.api (+ a dummy api_key). Without
#     an explicit providers.ollama block the profile falls back to openrouter and
#     401s (no reply) — the single most load-bearing line here.
#   - `.no-bundled-skills` marker → drops Hermes' 105-skill bundled catalog so
#     each lean profile loads only its own pack (#291 bloat fix).
#   - household: gemma4:e2b, the resident-facing sol-* skills, servicebay-mcp +
#     gatekeeper-mcp (NO servicebay_admin). admin: gemma4:12b, the sol-admin-*
#     pack, servicebay_admin + servicebay-mcp.
# ════════════════════════════════════════════════════════════════════════════

# A profile's MCP entries are (name, url, token); a token-less entry gets no
# headers (an empty Bearer makes Hermes reject it — same rule as render_mcp_block).
#
# The household persona is the DEFAULT profile (served by the container's bare
# `gateway run` on :8642; its solilos skills are already bind-mounted in the
# default home's /opt/data/skills and its mcp is merged into the global config),
# so there is no separate `household` profile. Only `admin` is a named profile.
ADMIN_PROFILE = "admin"
# The shared, bind-mounted admin skill pack symlinked into the admin profile.
ADMIN_SKILL_PACK = "admin-soul"


def render_ollama_model_block(provider_url: str, model: str) -> str:
    """Render the per-profile `model:` + `providers.ollama:` blocks.

    Box-confirmed: the profile must declare `model.provider: ollama` AND a
    matching `providers.ollama.api` (the Ollama OpenAI base) + an api_key, or
    Hermes falls back to openrouter and 401s. The api_key is a documented dummy
    — Ollama ignores it — but Hermes refuses an empty one."""
    return (
        "model:\n"
        "  provider: ollama\n"
        f"  model: {model}\n"
        "providers:\n"
        "  ollama:\n"
        f"    api: {provider_url}\n"
        '    api_key: "ollama"\n'
    )


# The disabled-toolsets list is shared by both profiles (the #230/#268 cold-cache
# prefill trim); cronjob STAYS enabled (timers/alarms/reminders + the 3 crons).
_DISABLED_TOOLSETS = (
    "browser",
    "code_execution",
    "image_gen",
    "video_gen",
    "delegation",
    "discord_admin",
    "x_search",
    "yuanbao",
    "moa",
    "computer_use",
    "kanban",
)


def _profile_container_dir(profile: str) -> str:
    """The profile's home INSIDE the hermes container (/opt/data/profiles/<p>)."""
    return f"/opt/data/profiles/{profile}"


def write_file_in_container(container_path: str, content: str) -> bool:
    """Write a file INSIDE the hermes container via `podman exec -i` so it lands
    owned by the hermes user.

    Per-profile dirs are created by `hermes profile create` inside the container
    and are hermes-owned, mode 0700 — the post-deploy runs as a different host
    uid, so its host-side open()/makedirs() into a profile dir silently FAIL
    (box-verified #293). Every per-profile file write therefore goes through the
    container, same as write_config_via_container. Creates the parent dir.
    Returns True on success."""
    try:
        proc = subprocess.run(
            [
                "podman",
                "exec",
                "-i",
                HERMES_CONTAINER,
                "sh",
                "-c",
                f"mkdir -p $(dirname {container_path}) && cat > {container_path}",
            ],
            input=content,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as e:
        jlog(
            "error",
            "profile:write",
            "could not exec container file write",
            path=container_path,
            error=str(e),
        )
        return False
    if proc.returncode != 0:
        jlog(
            "error",
            "profile:write",
            "container file write failed",
            path=container_path,
            stderr=proc.stderr.strip(),
        )
        return False
    return True


def read_file_in_container(container_path: str) -> str | None:
    """Read a file from inside the hermes container (None if absent / exec
    fails)."""
    try:
        proc = subprocess.run(
            ["podman", "exec", HERMES_CONTAINER, "cat", container_path],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout if proc.returncode == 0 else None


def write_profile_env_port(profile: str, port: str) -> bool:
    """Pin a profile gateway's API-server port via its per-profile `.env`
    (`API_SERVER_PORT=<port>`), the ONLY lever that actually works (box-verified
    2026-06-09, #293).

    The container hard-sets `API_SERVER_PORT=8642` in the pod env; the image's
    s6 `gateway-<profile>/run` script runs `with-contenv`, so that env OVERRIDES
    any `api_server:` / `platforms.api_server.port` written into the profile
    `config.yaml` — every started profile gateway would otherwise collide on
    :8642 ("Port 8642 already in use"). Hermes loads the per-profile `.env` with
    override, so a sibling `.env` carrying `API_SERVER_PORT=<port>` is what binds
    the admin gateway to its own port (:8643). Written via the container (the
    profile dir is unwritable host-side). Returns True on write."""
    if not port:
        return False
    return write_file_in_container(
        f"{_profile_container_dir(profile)}/.env", f"API_SERVER_PORT={port}\n"
    )


def render_profile_config_yaml(
    provider_url: str,
    model: str,
    mcp_servers: list[tuple[str, str, str]],
) -> str:
    """Build a full per-profile config.yaml string: the ollama model+providers
    block (#293a critical recipe), holographic memory, the shared display +
    disabled_toolsets, and the profile's own mcp_servers block. The gateway's
    bind PORT is NOT set here — it comes from the per-profile `.env`
    (`API_SERVER_PORT`, see write_profile_env_port), because the container env
    overrides any api_server port written into config.yaml (box-verified #293)."""
    parts = [
        "# Written by ServiceBay's solilos template post-deploy.py (per-profile, #293).\n",
        "# Edit and restart the solilos service to apply.\n",
        "timezone: Europe/Berlin\n",
        render_ollama_model_block(provider_url, model),
    ]
    parts.extend(
        [
            "memory:\n  provider: holographic\n",
            "tts:\n  provider: piper\n",
            "browser:\n  engine: disabled\n",
            "model_catalog:\n  enabled: false\n",
            "network:\n  force_ipv4: true\n",
            "display:\n  personality: default\n  show_reasoning: false\n",
            "agent:\n  disabled_toolsets:\n",
        ]
    )
    for toolset in _DISABLED_TOOLSETS:
        parts.append(f"    - {toolset}\n")
    block = render_mcp_block(mcp_servers)
    if block:
        parts.append("\n" + block)
    return "".join(parts)


def hermes_profile_create(profile: str, no_skills: bool = False) -> bool:
    """`hermes profile create <name>` via the hermes container, idempotently.
    Treats an already-exists exit as success (Hermes prints "already exists" and
    exits non-zero) so a redeploy is a no-op. Returns True when the profile is
    present afterwards (created or pre-existing).

    `no_skills=True` passes `--no-skills` so the image creates an EMPTY profile
    (no ~105-skill bundled-catalog copy + opts out of `hermes update` skill sync)
    — the box-verified way to keep a profile lean. Don't pre-place a marker to
    suppress bundled skills: `create` ERRORS if the profile dir already exists
    (#293)."""
    cmd = ["podman", "exec", HERMES_CONTAINER, "hermes", "profile", "create", profile]
    if no_skills:
        cmd.append("--no-skills")
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as e:
        jlog(
            "warn",
            "profile:create",
            "could not exec hermes profile create",
            profile=profile,
            error=str(e),
        )
        return False
    out = (proc.stdout + proc.stderr).lower()
    if proc.returncode == 0:
        jlog("info", "profile:create", "created hermes profile", profile=profile)
        return True
    if "already" in out and "exist" in out:
        jlog("info", "profile:create", "hermes profile already exists", profile=profile)
        return True
    jlog(
        "warn",
        "profile:create",
        "hermes profile create failed",
        profile=profile,
        returncode=proc.returncode,
        stderr=proc.stderr.strip(),
    )
    return False


def hermes_gateway_start(
    profile: str, attempts: int = 3, backoff_s: float = 3.0
) -> bool:
    """`hermes -p <profile> gateway start` via the hermes container — bring a
    secondary profile's gateway UP ALONGSIDE the container's default `gateway
    run`, in the SAME container (#293, box-validated 2026-06-09). The image's
    /etc/cont-init.d/02-reconcile-profiles manages the started gateway's own s6
    slot, so this does NOT crash-loop s6 the way overriding the container
    `command:` with `-p` did (the #294 trap). Idempotent: an already-running
    gateway exits non-zero with an "already running" message, treated as success.
    Retries a few times for the just-restarted-Hermes readiness race. Returns
    True when the gateway is up afterwards."""
    for attempt in range(1, attempts + 1):
        try:
            proc = subprocess.run(
                [
                    "podman",
                    "exec",
                    HERMES_CONTAINER,
                    "hermes",
                    "-p",
                    profile,
                    "gateway",
                    "start",
                ],
                capture_output=True,
                text=True,
                timeout=60,
            )
        except (OSError, subprocess.SubprocessError) as e:
            jlog(
                "warn",
                "profile:gateway-start",
                "could not exec hermes gateway start",
                profile=profile,
                attempt=attempt,
                error=str(e),
            )
            if attempt < attempts:
                time.sleep(backoff_s)
            continue
        out = (proc.stdout + proc.stderr).lower()
        if proc.returncode == 0:
            jlog(
                "info",
                "profile:gateway-start",
                "started profile gateway alongside the default",
                profile=profile,
            )
            return True
        if "already" in out and ("run" in out or "start" in out):
            jlog(
                "info",
                "profile:gateway-start",
                "profile gateway already running",
                profile=profile,
            )
            return True
        jlog(
            "warn",
            "profile:gateway-start",
            "hermes gateway start failed",
            profile=profile,
            attempt=attempt,
            returncode=proc.returncode,
            stderr=proc.stderr.strip(),
        )
        if attempt < attempts:
            time.sleep(backoff_s)
    return False


def symlink_profile_skill(profile: str, skill_name: str) -> bool:
    """Make a named profile see a shared, bind-mounted skill pack by symlinking it
    into the profile's own skills dir (#293 — "share the same drive"). A named
    Hermes profile loads skills from /opt/data/profiles/<profile>/skills, but the
    Solilos packs are bind-mounted at /opt/data/skills/<name> (the DEFAULT
    profile's home), so a named profile can't see them without this bridge.
    Created via `podman exec` so the symlink target resolves inside the container
    namespace (the host has a different /opt/data path). Idempotent. Returns True
    when the link was created."""
    target = f"/opt/data/skills/{skill_name}"
    link = f"/opt/data/profiles/{profile}/skills/{skill_name}"
    try:
        proc = subprocess.run(
            [
                "podman",
                "exec",
                HERMES_CONTAINER,
                "sh",
                "-c",
                f"mkdir -p /opt/data/profiles/{profile}/skills && ln -sfn {target} {link}",
            ],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as e:
        jlog(
            "warn",
            "profile:skill-link",
            "could not symlink shared skill into profile",
            profile=profile,
            skill=skill_name,
            error=str(e),
        )
        return False
    if proc.returncode != 0:
        jlog(
            "warn",
            "profile:skill-link",
            "symlink exec failed",
            profile=profile,
            skill=skill_name,
            stderr=proc.stderr.strip(),
        )
        return False
    jlog(
        "info",
        "profile:skill-link",
        "linked shared skill pack into profile",
        profile=profile,
        skill=skill_name,
    )
    return True


def write_profile_config(profile: str, content: str) -> bool:
    """Write a profile's config.yaml via the container (the profile dir is
    hermes-owned 0700, unwritable host-side — #293). Returns True on write."""
    if write_file_in_container(
        f"{_profile_container_dir(profile)}/config.yaml", content
    ):
        jlog("info", "profile:config", "wrote profile config.yaml", profile=profile)
        return True
    return False


def write_profile_soul(profile: str, soul_container_source: str) -> bool:
    """Install a profile's SOUL.md from a shipped source, via the container
    (the profile dir is unwritable host-side — #293), preserving an operator-
    edited soul. `hermes profile create` drops a stock soul; we overwrite that
    and our own previously-shipped soul, but leave a hand-customised one
    untouched. Returns True on write.

    `soul_container_source` is read THROUGH the container (a bind-mounted
    in-container path, e.g. /opt/data/skills/admin-soul/SOUL.md): the post-deploy
    can't rely on a host-side template path because ServiceBay's staging dir may
    not carry the skills/ subtree (box-verified #293 — the host read silently
    returned nothing and the stock soul stuck)."""
    soul = read_file_in_container(soul_container_source)
    if not soul:
        jlog(
            "warn",
            "profile:soul",
            "shipped SOUL.md not readable in container — skipping",
            source=soul_container_source,
        )
        return False
    target = f"{_profile_container_dir(profile)}/SOUL.md"
    existing = read_file_in_container(target) or ""
    if existing == soul:
        return False
    # Overwrite the stock soul Hermes drops at `profile create` (it is the
    # "You are Hermes Agent … created by Nous Research" text, NOT the
    # STOCK_SOUL_MARKER heading the first-boot stock soul uses) and our own
    # previously-shipped soul; preserve a genuine operator hand-edit.
    is_stock = (
        STOCK_SOUL_MARKER in existing
        or "Nous Research" in existing
        or "You are Hermes Agent" in existing
    )
    is_ours = "Solilos" in existing or "operator" in existing.lower()
    if existing.strip() and not is_stock and not is_ours:
        jlog(
            "info",
            "profile:soul",
            "leaving customised profile SOUL.md untouched",
            profile=profile,
        )
        return False
    if write_file_in_container(target, soul):
        jlog("info", "profile:soul", "installed profile SOUL.md", profile=profile)
        return True
    return False


def admin_mcp_servers() -> list[tuple[str, str, str]]:
    """The admin profile's MCP entries: servicebay_admin (read+lifecycle+mutate)
    + servicebay-mcp. Mints the admin bearer with mint_admin_token (the same
    full-admin token the admin-soul splice uses)."""
    servers: list[tuple[str, str, str]] = []
    if SERVICEBAY_MCP_URL:
        token = mint_admin_token()
        if token:
            servers.append((ADMIN_MCP_NAME, SERVICEBAY_MCP_URL, token))
        else:
            jlog(
                "warn",
                "profile:admin-mcp",
                "servicebay_admin skipped",
                reason="admin token mint failed (will retry on next redeploy)",
            )
        current = existing_servicebay_mcp_token()
        if current and probe_servicebay_mcp_token(current):
            servers.append(("servicebay-mcp", SERVICEBAY_MCP_URL, current))
        else:
            sb_token = mint_servicebay_mcp_token()
            if sb_token:
                servers.append(("servicebay-mcp", SERVICEBAY_MCP_URL, sb_token))
    else:
        jlog("info", "profile:admin-mcp", "admin MCP skipped", reason="missing url")
    return servers


def provision_profiles(data_dir: str, provider_url: str, admin_port: str = "") -> None:
    """Provision the ONE named profile — `admin` — that runs alongside the
    household default in the same container (#293, native-reconciler design).

    The household persona IS the DEFAULT profile (served by the bare `gateway
    run` on :8642; its config is the global /opt/data/config.yaml and its solilos
    skills are already mounted at /opt/data/skills — no separate profile, no
    `profile use`). So only admin needs provisioning: `hermes profile create
    --no-skills` (an EMPTY, lean profile — no ~105-skill bundled-catalog copy),
    then its config.yaml (ollama model+providers, gemma4:12b, servicebay_admin +
    servicebay-mcp), a per-profile `.env` pinning API_SERVER_PORT so its gateway
    binds :8643 (the config.yaml port is ignored — the container env overrides it,
    #293), its admin-soul pack symlinked in from the shared bind mount, and its
    SOUL.md. EVERY per-profile file is written via the container (the profile dir
    is hermes-owned 0700 and unwritable host-side — box-verified #293). Idempotent
    + fail-soft. The admin gateway is brought up by start_admin_gateway after the
    final restart; the image's container_boot reconciler restarts it on every
    subsequent boot (its recorded gateway_state is `running`). Trimming the
    existing default/household profile's bundled skills is tracked separately
    (#291); household = default works as-is here.

    `data_dir` is accepted for signature symmetry with the other phases but the
    admin profile is addressed by its in-container path (the host path is
    unwritable), so it is unused."""
    del data_dir  # admin files are written via the container, not host paths
    admin_model = env("ADMIN_PROFILE_MODEL", "gemma4:12b")

    hermes_profile_create(ADMIN_PROFILE, no_skills=True)
    mcp_servers = admin_mcp_servers()
    write_profile_config(
        ADMIN_PROFILE,
        render_profile_config_yaml(provider_url, admin_model, mcp_servers),
    )
    write_profile_env_port(ADMIN_PROFILE, admin_port)
    symlink_profile_skill(ADMIN_PROFILE, ADMIN_SKILL_PACK)
    # Read the operator soul from the bind-mounted admin-soul pack IN the
    # container (host staging may omit skills/, box-verified #293).
    write_profile_soul(ADMIN_PROFILE, f"/opt/data/skills/{ADMIN_SKILL_PACK}/SOUL.md")
    jlog(
        "info",
        "profile:provision",
        "provisioned admin profile",
        model=admin_model,
        api_port=admin_port,
        mcp_servers=[name for name, _, _ in mcp_servers],
    )


def start_admin_gateway() -> None:
    """Bring the admin profile's gateway up alongside the container's default
    (household) `gateway run` (#293). Called AFTER the final restart so the
    restart can't wipe the started gateway. Tolerant + idempotent
    (hermes_gateway_start retries + treats already-running as success)."""
    if not hermes_gateway_start(ADMIN_PROFILE):
        jlog(
            "warn",
            "profile:gateway-start",
            "admin gateway not confirmed up — the admin embed may be unreachable until the next redeploy; check `hermes profile list` on the box",
            profile=ADMIN_PROFILE,
        )


# ════════════════════════════════════════════════════════════════════════════
# 5. THE SINGLE FINAL RESTART — POST /api/services/solilos/action {restart}.
# ════════════════════════════════════════════════════════════════════════════


def restart_solilos(sb_api: str) -> bool:
    """POST /api/services/solilos/action {action: 'restart'} so all containers
    pick up the final config.yaml + .env. Risk-2-safe (#271 spike): SB runs
    this script in an SSH session and the restart is `--no-block` async, so the
    queued restart does not kill the running post-deploy."""
    status, body = post_json(
        f"{sb_api}/api/services/{SOLILOS_SERVICE}/action",
        {"action": "restart"},
        timeout=30,
    )
    if status == 200:
        jlog("info", "solilos:restart", "restart requested via ServiceBay API")
        return True
    err = (body or {}).get("error") if isinstance(body, dict) else None
    jlog(
        "warn",
        "solilos:restart",
        "restart request failed; the config will take effect on next manual restart",
        status=status,
        error=str(err) if err else None,
    )
    return False


# ════════════════════════════════════════════════════════════════════════════
# main — the ordered sequence.
# ════════════════════════════════════════════════════════════════════════════


def main() -> int:
    global \
        SB_API_URL, \
        HERMES_API_PORT, \
        HERMES_API_KEY, \
        HERMES_API_URL, \
        SERVICEBAY_MCP_URL, \
        GATEKEEPER_MCP_URL, \
        GATEKEEPER_MCP_TOKEN, \
        READINESS_TIMEOUT_S

    data_dir = env("DATA_DIR", "/mnt/data")
    sb_api = env("SB_API_URL", "http://localhost:3000").rstrip("/")
    host = env("HOST", "<server-ip>")
    api_port = env("HERMES_API_PORT", "8642")
    admin_api_port = env("HERMES_ADMIN_API_PORT", "8643")
    api_key = env("HERMES_API_KEY")
    provider_url = env("HERMES_LLM_PROVIDER_URL", "http://127.0.0.1:11434/v1")
    model = env("OLLAMA_DEFAULT_MODEL", "gemma4:12b")
    dashboard_port = env("HERMES_DASHBOARD_PORT")
    honcho_port = env("HONCHO_PORT")
    honcho_api_key = env("HONCHO_API_KEY")

    # Wire the module-level globals the solbay/admin phases read.
    SB_API_URL = sb_api
    HERMES_API_PORT = api_port
    HERMES_API_KEY = api_key
    HERMES_API_URL = f"http://127.0.0.1:{api_port}"
    SERVICEBAY_MCP_URL = os.environ.get("SERVICEBAY_MCP_URL", "")
    # The gatekeeper MCP server always listens on the deterministic in-pod port
    # (gatekeeper container MCP_PORT, hard-coded 10760). Default the URL when
    # the variable is absent so gatekeeper-mcp is still registered.
    GATEKEEPER_MCP_URL = (
        os.environ.get("GATEKEEPER_MCP_URL", "") or "http://127.0.0.1:10760/mcp"
    )
    GATEKEEPER_MCP_TOKEN = os.environ.get("GATEKEEPER_MCP_TOKEN", "")
    READINESS_TIMEOUT_S = int(os.environ.get("HERMES_READINESS_TIMEOUT_S", "120"))

    # ── 1. hermes phase ──────────────────────────────────────────────────────
    config_path = write_config_yaml(
        data_dir,
        provider_url,
        model,
        honcho_port=honcho_port,
        honcho_api_key=honcho_api_key,
    )
    if config_path:
        ensure_sb_mcp_servers_block(config_path, sb_api)

    write_soul_md(data_dir)

    ha_token = adopt_ha_long_lived_token(data_dir)
    if ha_token:
        ensure_ha_jellyfin_integration(
            ha_token,
            env("JELLYFIN_URL"),
            env("JELLYFIN_USERNAME"),
            env("JELLYFIN_PASSWORD"),
        )

    write_gateway_env(
        data_dir,
        {
            "TELEGRAM_BOT_TOKEN": env("TELEGRAM_BOT_TOKEN"),
            "TELEGRAM_ALLOWED_USERS": env("TELEGRAM_ALLOWED_USERS"),
            "DISCORD_BOT_TOKEN": env("DISCORD_BOT_TOKEN"),
            "DISCORD_ALLOWED_CHANNELS": env("DISCORD_ALLOWED_CHANNELS"),
            "SIGNAL_ACCOUNT": env("SIGNAL_ACCOUNT"),
            "SIGNAL_ALLOWED_USERS": env("SIGNAL_ALLOWED_USERS"),
        },
    )

    write_ddgs_install_script(data_dir)

    # ── 2. chat phase ────────────────────────────────────────────────────────
    # decommission's data_dir default differed (`/mnt/data/stacks`); the merged
    # platform DATA_DIR is authoritative, so use it for the archive root too.
    decommission(sb_api, data_dir)

    # ── 3. solbay phase ──────────────────────────────────────────────────────
    wait_for_hermes()
    register_chronicle_cron()
    register_problem_summarizer_cron()
    register_chat_compactor_cron()
    # Mirror the household MCP set into the global /opt/data/config.yaml too: the
    # config-agent panel (model switch) reads that file, and the `default`
    # profile uses it before `profile use household` takes effect. The
    # household-facing gateway itself loads its OWN per-profile config (#293).
    servers = collect_mcp_servers()
    merged = merge_config_yaml(servers)
    if not merged:
        jlog(
            "warn",
            "solbay:config",
            "global mcp_servers merge skipped — config.yaml not readable via the container yet",
        )

    # ── 4. profile provisioning (#293) ───────────────────────────────────────
    # household IS the default profile (served by the bare `gateway run` on
    # :8642, config = the global config.yaml just written, solilos skills already
    # mounted). The ONLY named profile is `admin`: gemma4:12b, lean admin-soul
    # pack symlinked in, servicebay_admin ONLY here (the #268 structural fix), and
    # its bind port pinned via a per-profile `.env` (API_SERVER_PORT=:8643 — the
    # config.yaml port is overridden by the container env, box-verified #293).
    provision_profiles(data_dir, provider_url, admin_port=admin_api_port)
    # Boot hook that keeps admin up across reboots (#299) — written via the
    # container (hermes is up now, after wait_for_hermes) so it lands on the
    # volume BEFORE the restart below recreates the pod with the hook mounted.
    write_admin_gateway_boot_hook()

    # ── 5. restart, THEN start the admin gateway ─────────────────────────────
    # The restart makes the container's bare `gateway run` serve the household
    # default on :8642. The admin gateway is started AFTER (and after a fresh
    # readiness wait) so the restart can't wipe it — it runs alongside the
    # household gateway in the same container on :8643 (#293, s6-safe). On every
    # later boot the image's container_boot reconciler restarts admin (its
    # recorded gateway_state is `running`).
    time.sleep(3)
    restart_solilos(sb_api)
    time.sleep(5)
    wait_for_hermes()
    start_admin_gateway()

    # Surface the API key for downstream wiring (MCP clients, operator scripts).
    if api_key:
        emit_credential(
            service="Solilos (Hermes API)",
            url=f"http://{host}:{api_port}",
            username="(bearer token)",
            password=api_key,
            importance="critical",
            notes="Bearer token for Hermes' API. Send as `Authorization: Bearer <key>`. Regenerate from the wizard if it leaks.",
        )

    print(
        f"✅ Solilos is configured: model={model}, provider={provider_url}, port={api_port}."
    )
    if dashboard_port:
        print(
            f"   Hermes dashboard on 127.0.0.1:{dashboard_port} — see README for the NPM + Authelia setup."
        )
    print("   Chat surface + gatekeeper voice bridge run in the same Pod.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""
post-deploy hook for the `hermes` template.

Three responsibilities:

  1. **Write config.yaml.** Hermes reads its model-provider settings
     from /opt/data/config.yaml. The upstream entrypoint copies a
     default config.yaml on first start if none exists. We overwrite
     it with the wizard-collected provider/model/base_url so Hermes
     points at the ServiceBay `ollama` template (or whatever endpoint
     the operator picked).

  2. **Restart the pod** so Hermes picks up the new config. We do
     this via ServiceBay's own POST /api/services/<name>/action
     endpoint rather than `systemctl` so the restart counts as a
     managed action and shows in the service history.

  3. **Surface HERMES_API_KEY** as a __SB_CREDENTIAL__ marker so it
     lands in the wizard's SAVE-THESE-NOW banner. Operators paste it
     into Solilos's solbay config (or any other client) to
     authenticate against Hermes' API.

UX_PHILOSOPHY.md § 2 bans operator-facing `podman exec`
instructions. Everything Hermes needs to come up wired to Ollama is
done here, without any interactive step.

See lib/registry.ts:getTemplatePostDeployScript for the script
protocol.
"""

from __future__ import annotations

import datetime
import json
import os
import re
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


def _honcho_health_timeout() -> int:
    return int(os.environ.get("HONCHO_PROBE_TIMEOUT", "5"))


def detect_honcho(port: str) -> bool:
    """#1004 — Probe http://127.0.0.1:<HONCHO_PORT>/health. Returns True
    when reachable + 2xx, False otherwise. Short timeout: the honcho
    template's own post-deploy already waited for /health, so by the
    time hermes' post-deploy runs we either get an immediate green
    answer or we accept that honcho isn't installed."""
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

    Returns a list of `name:tag` strings, or `[]` on any failure
    (caller falls back to leaving custom_providers.models empty, which
    Hermes will then auto-detect via /v1/models — slower but functional).
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
    """Build a YAML `custom_providers:` block that lists every Ollama tag
    on the host under a single `ollama` named provider, so Hermes'
    dashboard Models tab surfaces them as one-click switches.

    The format matches the documented schema at
    https://hermes-agent.nousresearch.com/docs/integrations/providers#named-custom-providers:

        custom_providers:
          - name: ollama
            base_url: http://127.0.0.1:11434/v1
            api_key: none
            models:
              gemma4:12b: {}
              gemma4:e2b: {}
              VladimirGav/gemma4-26b-16GB-VRAM:latest: {}

    Empty per-model mappings leave context_length / api_mode for Hermes
    to auto-detect via `/v1/models`. The point of explicitly listing the
    tags is solely that the dashboard's typeahead surfaces them — the
    Hermes Models tab doesn't query `/v1/models` for tag enumeration,
    it reads from the model_catalog + custom_providers.

    Returns '' if there are no tags (caller skips the block — an empty
    block is YAML-invalid because `models:` would have no children).
    """
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
        # YAML keys containing `:` are unambiguous when they're the
        # entire key (no flow context, no anchors), but a literal `: `
        # mid-value would terminate the key. Ollama tags only use `:`
        # as a name/tag separator (`gemma4:12b`); no space follows. Safe
        # to emit unquoted. Empty mapping `{}` means "no overrides".
        out.append(f"      {tag}: {{}}\n")
    return "".join(out)


def _extract_top_level_block(content: str, key: str) -> str:
    """Extract a top-level YAML block by key from config.yaml content,
    returning the block (header + indented body) as a string, or '' if
    not present. Mirrors strip_mcp_servers_block() in
    templates/solbay/post-deploy.py — kept as a separate helper
    here so this template doesn't import across template directories.

    "Top-level" means the key starts at column 0; the block ends at the
    next top-level key (or EOF). Comments and blank lines inside the
    block are preserved.
    """
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
        # In-block: keep blank/indented lines; break on next top-level key.
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
    """Write /opt/data/config.yaml's model: block. Returns the path on
    success, None if the write failed. Best-effort — a failure here
    means Hermes falls back to its default config.yaml (likely empty
    model.base_url), which the operator can fix from the wizard.

    Preserves an existing `mcp_servers:` block if present (#1045) — the
    SB-MCP auto-wiring writes that block once on first install, and
    every subsequent hermes/post-deploy run would otherwise erase it
    (the pre-2026-05-26 behaviour the solbay README still
    warns about: "re-deploying the `hermes` template overwrites
    config.yaml … re-deploy solbay afterwards to restore").
    """
    config_dir = os.path.join(data_dir, "hermes")
    config_path = os.path.join(config_dir, "config.yaml")
    # Stash the existing mcp_servers block (if any) so the rewrite below
    # doesn't erase it. See #1045 / the solbay README caveat.
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
    # #1002 — ServiceBay defaults table. Six overrides on top of
    # Hermes' upstream defaults that swing the box from "developer
    # laptop" toward "household appliance" semantics. See the
    # discussion thread on #1002 for the why of each:
    #   - memory.provider — `honcho` when the honcho template is up
    #     (#1004 detect-then-configure below); `holographic` otherwise.
    #     Holographic is local-only with no external deps and is fine
    #     for a single-operator install. Honcho gives per-LLDAP-user
    #     memory isolation for multi-resident households.
    #   - tts.provider=piper — local voice. Falls back to '' (silent)
    #     when piper isn't installed yet; never the upstream `edge`
    #     (Microsoft online) default.
    #   - browser.engine=disabled — agents inside a container almost
    #     never want a real browser. Skills that need it flip it on.
    #   - model_catalog.enabled=false — don't phone the upstream
    #     catalog endpoint every 24h. Privacy-by-default.
    #   - network.force_ipv4=true — FritzBox + IPv6 has bitten other
    #     services (#415). Skip AAAA records in aiohttp.
    #   - display.personality=default — household assistant, not anime.
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
    # Enumerate Ollama tags so the dashboard Models tab can show them as
    # switchable entries — without an explicit `custom_providers.ollama.models`
    # list, the tab's typeahead falls back to remote provider catalogs
    # only and local Ollama tags are invisible. `[]` (probe failed) is
    # fine — we just skip the block and the operator works around it
    # via `hermes config set model.model <tag>` like before.
    ollama_tags = enumerate_ollama_tags(provider_url)
    custom_providers_block = render_custom_providers_block(provider_url, ollama_tags)

    content = (
        "# Written by ServiceBay's hermes template post-deploy.py.\n"
        "# Edit via the wizard's reconfigure flow or hand-edit and restart the hermes service.\n"
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
        # Global default OFF (#222/#224): reasoning is surfaced PER REQUEST by
        # the proxies (they send `show_reasoning: true` only on a thorough turn),
        # so fast tool turns stay clean and a thorough turn still renders its
        # thinking. Leaving this false avoids surfacing an (empty) block on the
        # common fast turn.
        "  show_reasoning: false\n"
        # Cold-cache prefill floor (#230): the ~29k-token system prompt is
        # dominated by the cumulative built-in tool-definition JSON, not — as
        # first suspected — an inlined HA entity-state dump (the `homeassistant`
        # toolset only registers ha_list_entities/ha_get_state/ha_list_services/
        # ha_call_service and fetches state lazily; the state-pushing HA *gateway*
        # is `watch_*`-gated and OFF by default). Hermes enables ~15 toolsets by
        # default for every session; blacklist the ones the household assistant
        # never uses so their tool schemas (and any associated prompt guidance,
        # e.g. memory's MEMORY_GUIDANCE) drop out of every prefill. Kept:
        # homeassistant (device control), skills, memory, web/search (ddgs),
        # vision (media-ingestion), tts (voice), todo, file + terminal (skills
        # write notes via write_file/replace_file_content and ripgrep the vault
        # via the terminal tool — verified in the SKILL.md bodies), cronjob,
        # session_search, clarify, safe. Disabled = verifiably unused here:
        #   - browser: engine is `disabled` above, so its tool defs are dead weight
        #   - code_execution: dynamic-skills explicitly forbids run_command; no
        #     household skill executes code
        #   - image_gen: we ingest images (vision), never generate them
        #   - delegation: no multi-agent delegation in the household path
        "agent:\n"
        "  disabled_toolsets:\n"
        "    - browser\n"
        "    - code_execution\n"
        "    - image_gen\n"
        "    - delegation\n"
    )
    if custom_providers_block:
        content += "\n" + custom_providers_block
    if preserved_mcp_servers:
        # Append the stashed mcp_servers block so SB-MCP / HA-MCP wiring
        # survives a hermes-only redeploy. Add a separator blank line so
        # the block stays visually distinct from `display:` above.
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
    # Make the dir traversable and the file readable so OTHER templates'
    # post-deploys (notably solbay) can splice an `mcp_servers:`
    # block into the same config.yaml. Without this, the hermes container
    # leaves the dir as 0o700 and downstream post-deploys silently bail
    # at os.path.exists() with "config.yaml not found" - observed
    # 2026-05-25 during a household-stack install: ha-mcp + servicebay-mcp
    # never got wired, so Hermes ran with the model block only and could
    # neither control Home Assistant nor query ServiceBay logs.
    # Best-effort: a PermissionError here is non-fatal (other-readability
    # is a convenience, not a hard requirement for Hermes itself).
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
# A file that still carries it is "unclaimed" — we replace it with the
# Solilos soul. A soul an operator (or the panel) has customised never
# carries this marker, so it is left untouched.
STOCK_SOUL_MARKER = "# Hermes Agent Persona"


def write_soul_md(data_dir: str) -> bool:
    """Install the Solilos SOUL.md (Hermes' durable identity) when the box
    still carries Hermes' stock default or has none yet. Reads the soul
    shipped alongside this script. Never overwrites a customised soul.
    Returns True when the file was written (signals the caller to restart so
    Hermes reloads it)."""
    target = os.path.join(data_dir, "hermes", "SOUL.md")
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
        return False  # already the Solilos soul — idempotent no-op
    if existing.strip() and STOCK_SOUL_MARKER not in existing:
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
    jlog("info", "hermes:soul", "installed Solilos SOUL.md", path=target)
    return True


def _provision_sb_mcp_token_once(sb_api: str, token_name: str) -> str | None:
    """One mint attempt against the canonical api-tokens route. Returns the
    `sb_`-shaped secret, or None on any failure."""
    # Canonical route; `/api/system/mcp-tokens` is only an alias on newer
    # ServiceBay and 404s on some versions (#126).
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


def provision_sb_mcp_token(sb_api: str, token_name: str = "hermes-mcp") -> str | None:
    """Mint a long-lived SB-MCP token via the ServiceBay HTTP API and
    return the bearer secret. Uses the `X-SB-Internal-Token` server-to-
    server header that `post_json` already plumbs.

    Scopes (#1045):
      - `read`      — Hermes can introspect ServiceBay state (services,
                      health, logs, twin) for "what's running?" / "why is
                      X failing?" questions.
      - `lifecycle` — Hermes can restart / reload misbehaving services
                      from chat without bouncing the user back to the
                      ServiceBay dashboard.

    Retries a few times with a short backoff — the usual cause of a
    transient failure is the SB-on-loopback readiness race (#126). Returns
    the freshly-minted `sb_`-shaped secret on success, None when every
    attempt failed. The caller must NOT persist any non-`sb_` fallback.
    """
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
    `initialize`. 200 = registered + accepted; 401 = stale/junk. A
    connection failure returns True so a transient loopback hiccup doesn't
    trigger a needless re-mint when the shape already passed (#126)."""
    if not token:
        return False
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "hermes-post-deploy", "version": "1"},
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
    """Pull the `servicebay:` entry's bearer token out of an mcp_servers
    block string. Returns the token (any shape) or None if absent."""
    in_sb = False
    for line in mcp_block.splitlines():
        stripped = line.strip()
        # Entry keys sit at 2-space indent; `servicebay:` opens its sub-block.
        if line[:2] == "  " and line[:3] != "   " and stripped.endswith(":"):
            in_sb = stripped == "servicebay:"
            continue
        if in_sb and stripped.startswith("Authorization:"):
            m = re.search(r"Bearer\s+(\S+)", stripped)
            return m.group(1).strip('"') if m else None
    return None


def ensure_sb_mcp_servers_block(config_path: str, sb_api: str) -> bool:
    """Ensure config.yaml carries a `mcp_servers.servicebay:` entry with a
    VALID bearer. Mints a fresh SB-MCP token and writes the entry inline.

    Self-heal (#126): a present `servicebay:` entry is NOT treated as done.
    We validate its bearer — it must be `sb_`-shaped and live-probe 200
    against /mcp — and re-mint + rewrite when it's invalid (a box that
    already persisted the junk fallback token self-corrects on redeploy).
    A still-valid token is left untouched (idempotent no-op).

    Returns True when the file was mutated (signals the caller to
    trigger a restart so Hermes re-reads the config).

    Note for Solilos coexistence: solbay's post-deploy currently
    *rewrites* the entire `mcp_servers:` block, which would overwrite
    this entry. The follow-up to make Solilos preserve existing entries
    instead of rewriting is tracked in the same #1045 thread.
    """
    if not os.path.exists(config_path):
        # write_config_yaml failed — nothing for us to do here.
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
            # Already wired with a valid, accepted token — nothing to do.
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
        # Replace the stale servicebay: sub-block in place, preserving any
        # other entries (e.g. home_assistant:) around it.
        healed_mcp = _replace_servicebay_entry(existing_mcp, new_entry_lines)
        new_content = existing.replace(existing_mcp, healed_mcp, 1)
    elif existing_mcp:
        # Block exists but no servicebay entry — append into it (preserves
        # other entries Solilos may have written).
        appended = existing_mcp.rstrip("\n") + "\n" + "".join(new_entry_lines)
        new_content = existing.replace(existing_mcp, appended, 1)
    else:
        # No existing mcp_servers — append a fresh block at EOF.
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
    """Swap the existing `servicebay:` sub-block in an mcp_servers block for
    `new_entry_lines`, preserving every other entry. The sub-block runs from
    the `  servicebay:` line to the next 2-space-indented entry key or EOF."""
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
            # Stay in the stale sub-block until the next entry key / dedent.
            if is_entry_key or (line.strip() and line[:1] not in (" ", "\t")):
                in_sb = False
                out.append(line)
            # else: still inside the stale servicebay block → drop
            continue
        out.append(line)
    return "".join(out)


def restart_hermes(sb_api: str) -> bool:
    """POST /api/services/hermes/action {action: 'restart'}. Best-effort."""
    status, body = post_json(
        f"{sb_api}/api/services/hermes/action",
        {"action": "restart"},
        timeout=30,
    )
    if status == 200:
        jlog("info", "hermes:restart", "restart requested via ServiceBay API")
        return True
    err = (body or {}).get("error") if isinstance(body, dict) else None
    jlog(
        "warn",
        "hermes:restart",
        "restart request failed; the config will take effect on next manual restart",
        status=status,
        error=str(err) if err else None,
    )
    return False


def write_gateway_env(data_dir: str, entries: dict[str, str]) -> bool:
    """Merge messaging-gateway credentials into `<DATA_DIR>/hermes/.env`.

    Hermes reads its gateway allowlists and bot tokens from this file at
    start time (the warning Hermes prints on a fresh install names this
    exact path: `~/.hermes/.env`). The pod mounts `<DATA_DIR>/hermes` at
    `/opt/data`, and Hermes' default HOME is `/opt/data` → `.env` here is
    the same file Hermes loads.

    Merge semantics: read existing key/value lines, overwrite the keys
    we manage, keep everything else untouched. Empty values clear a key
    (so an operator who removes a token from the wizard actually rotates
    the credential out). A run with all-empty inputs and no existing
    file is a no-op.

    Returns True when the .env file changed (signals the caller to
    restart the pod), False when it was already up to date.
    """
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
                        # Drop existing managed lines; they get rewritten below.
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

    # Decide whether anything would actually change.
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
    """Write the startup script to /etc/cont-init.d/99-install-ddgs (mounted path)
    so the ddgs python package is automatically installed when the container boots.
    Returns True when the file was written, False otherwise.
    """
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


# #1002 — Timeouts read lazily so the test suite can shrink them via
# env without import-order gymnastics. Mirrors the `LLDAP_READY_TIMEOUT`
# pattern in templates/auth/post-deploy.py.
def _ha_token_timeout() -> int:
    return int(os.environ.get("HA_TOKEN_TIMEOUT", "90"))


def _ha_api_timeout() -> int:
    return int(os.environ.get("HA_API_TIMEOUT", "60"))


def _wait_for_ha_token(token_path: str, deadline_secs: int | None = None) -> str | None:
    """#1002 — Poll for the HA long-lived token file. HA's post-deploy
    auto-onboards the `solilos` user and writes this file near the end of
    its run; if hermes' post-deploy is racing it (even with
    servicebay.dependencies: home-assistant) the file may not exist yet
    on first check. Returns the token once present + non-empty, or None
    if the deadline passes. A deadline of 0 means "check once, then
    give up" — used by the test suite to avoid the polling delay."""
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
    """#1002 — Probe HA's /api/ with the new token until it answers 200.
    Avoids the first reconnect-loop iteration where Hermes' HA gateway
    fires before HA's listener is fully up (the "Cannot connect to host
    127.0.0.1:8123" error in the v4.29.x install logs). Best-effort —
    we still restart even on timeout, since Hermes will retry on its
    own. A deadline of 0 means "skip the probe entirely" — used by the
    test suite."""
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
    """When home-assistant's post-deploy has auto-onboarded HA (#934), it
    leaves a long-lived access token at
    `<DATA_DIR>/home-assistant/homeassistant/.solilos-long-lived-token`.
    Pick that up over the placeholder `HASS_TOKEN` from assemble and
    patch the deployed hermes pod yml so Hermes' native HA gateway can
    actually authenticate. Returns the token on success, or None when
    the file never appears (operator opted out of auto-onboarding) or
    the patch was a no-op.

    #1002: now retries (up to 90s) for the token file and probes HA's
    /api/ before signalling ready. The previous one-shot read missed
    the file on every install where HA's auto-onboarding hadn't yet
    written it, leaving HASS_TOKEN as the placeholder."""
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
    # The hermes pod yml is written by ServiceBay's install runner to the
    # user-Quadlet directory. Patch the HASS_TOKEN env value in-place so a
    # subsequent restart picks up the real token. We match the YAML
    # structure ServiceBay produces:
    #     - name: HASS_TOKEN
    #       value: "<random>"
    pod_yml = os.path.expanduser("~/.config/containers/systemd/hermes.yml")
    if not os.path.exists(pod_yml):
        jlog(
            "warn",
            "hermes:ha-token",
            "hermes.yml not found at expected path",
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
            "could not read hermes.yml",
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
        # Already adopted on a previous run.
        return token
    try:
        with open(pod_yml, "w", encoding="utf-8") as f:
            f.write(new)
    except OSError as e:
        jlog(
            "warn",
            "hermes:ha-token",
            "could not write patched hermes.yml",
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

    # #1002 — Wait for HA's /api/ to answer 200 with this token before
    # we restart hermes. Without this gate Hermes' first HA-gateway
    # reconnect lands during HA's startup window and gets
    # "Cannot connect to host 127.0.0.1:8123" — operator-visible noise
    # that has nothing to do with the actual config.
    _wait_for_ha_api(token)
    return token


def _ha_get(path: str, token: str, timeout: float = 10.0) -> tuple[int, object]:
    """GET against HA's API with the long-lived token. Returns
    (status, parsed-json-or-None). 0 on connection failure."""
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
    """POST JSON against HA's API with the long-lived token. Returns
    (status, parsed-json-or-None). 0 on connection failure."""
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
    """Auto-install Home Assistant's `jellyfin` integration via HA's
    config-entries flow API so `media_player.jellyfin_*` entities appear —
    Sol then controls playback and reads now-playing through the existing
    `homeassistant` toolset (ha_call_service / ha_get_state) with zero
    Solilos code (#195).

    Idempotent self-heal: skips if a `jellyfin` config entry already exists.
    Fail-soft: a missing token/url/username, an unreachable Jellyfin, or any
    HA error logs + returns False — it never crashes the deploy.

    The live HA `jellyfin` config flow (confirmed against HA on the box) is a
    SINGLE `user` step whose schema is {url (required), username (required),
    password (optional)} — all submitted at once, not URL-then-auth.

    Returns True only when a new entry was created (a created config entry
    doesn't itself need a hermes restart, but the caller treats it like the
    other change signals for symmetry).
    """
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

    # Flow didn't complete (bad creds, unreachable Jellyfin, or a multi-step
    # change in a future HA). Abort the dangling flow so it doesn't linger,
    # then fail soft.
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


def main() -> int:
    data_dir = env("DATA_DIR", "/mnt/data")
    sb_api = env("SB_API_URL", "http://localhost:3000")
    host = env("HOST", "<server-ip>")
    api_port = env("HERMES_API_PORT", "8642")
    api_key = env("HERMES_API_KEY")
    provider_url = env("HERMES_LLM_PROVIDER_URL", "http://127.0.0.1:11434/v1")
    model = env("OLLAMA_DEFAULT_MODEL", "gemma4:12b")
    dashboard_port = env("HERMES_DASHBOARD_PORT")
    honcho_port = env("HONCHO_PORT")
    honcho_api_key = env("HONCHO_API_KEY")

    # 1. Write config.yaml with the wizard-picked provider + model, and
    # the memory provider chosen by detect-then-configure (#1004).
    config_path = write_config_yaml(
        data_dir,
        provider_url,
        model,
        honcho_port=honcho_port,
        honcho_api_key=honcho_api_key,
    )
    config_written = config_path is not None

    # 1b. SB-MCP auto-wiring (#1045). Mint a fresh SB-MCP token and
    # append a `mcp_servers.servicebay:` entry to config.yaml so Hermes
    # can introspect ServiceBay / restart misbehaving services from
    # chat without operator setup. Idempotent — re-runs see the
    # existing entry (preserved across rewrites by write_config_yaml's
    # mcp_servers stash) and skip.
    sb_mcp_changed = False
    if config_path:
        sb_mcp_changed = ensure_sb_mcp_servers_block(config_path, sb_api)

    # 1c. Install the Solilos soul (SOUL.md) over Hermes' stock default so
    # the assistant boots as Sol, not the generic "Hermes Agent Persona".
    # Skipped once a customised soul is in place (panel/operator edit).
    soul_written = write_soul_md(data_dir)

    # Pick up the real HA long-lived token if home-assistant's post-deploy
    # auto-onboarded HA. Without this Hermes' native HA gateway runs with
    # the random placeholder from `assemble` and gets `auth_invalid` from
    # HA on every call.
    ha_token = adopt_ha_long_lived_token(data_dir)

    # Auto-install HA's Jellyfin integration so Sol controls media playback +
    # reads now-playing via the existing `homeassistant` toolset, once
    # JELLYFIN_* are set (#195). Idempotent (skips if the entry exists) and
    # fail-soft (never crashes the deploy). Uses the same HA long-lived token
    # adopt_ha_long_lived_token just confirmed against HA's /api/.
    if ha_token:
        ensure_ha_jellyfin_integration(
            ha_token,
            env("JELLYFIN_URL"),
            env("JELLYFIN_USERNAME"),
            env("JELLYFIN_PASSWORD"),
        )

    # Merge messaging-gateway credentials (Telegram / Discord / Signal
    # allowlists + bot tokens) into <DATA_DIR>/hermes/.env. Idempotent —
    # only signals a restart when something actually changed.
    env_changed = write_gateway_env(
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

    # Write the ddgs install script to the host's config dir so it runs on pod start.
    ddgs_script_written = write_ddgs_install_script(data_dir)

    # 2. Restart so Hermes picks up the new config (and the new env if we
    # just patched HASS_TOKEN or rewrote .env).
    if (
        config_written
        or env_changed
        or sb_mcp_changed
        or soul_written
        or ddgs_script_written
    ):
        # Give the pod a few seconds to settle so the restart isn't
        # racing the initial deploy.
        time.sleep(3)
        restart_hermes(sb_api)

    # 3. Surface the API key for downstream wiring (solbay,
    # MCP clients, the operator's own scripts).
    if api_key:
        emit_credential(
            service="Hermes Agent (API)",
            url=f"http://{host}:{api_port}",
            username="(bearer token)",
            password=api_key,
            importance="critical",
            notes="Bearer token for Hermes' API. Send as `Authorization: Bearer <key>`. Bind a client by pasting this into solbay or your own MCP wiring. Regenerate from the wizard if it leaks.",
        )

    print(
        f"✅ Hermes is configured: model={model}, provider={provider_url}, port={api_port}."
    )
    if dashboard_port:
        print(
            f"   Dashboard enabled on 127.0.0.1:{dashboard_port} — see README for the NPM + Authelia setup."
        )
    print(
        f"   Other ServiceBay templates (solbay) can reach Hermes at http://127.0.0.1:{api_port}."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

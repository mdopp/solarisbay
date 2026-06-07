#!/usr/bin/env python3
"""Post-deploy hook for solbay.

Runs on the host (non-interactive) after the pod's containers report
Ready. Idempotent on every invocation.

What it does
============

1. **Wait for Hermes** to be ready by polling `GET /health` with the
   bearer token. The endpoint is documented at
   https://hermes-agent.nousresearch.com/docs/user-guide/features/api-server#endpoints.
   Whether `/health` is auth-exempt is undocumented as of writing; sending
   the bearer either way works as the strict superset.

1b. **Register the periodic crons** via Hermes' `POST /api/jobs` — the daily
   family-chronicle (#83) and the weekly troubleshooting-KB problem-summarizer
   (#182) — idempotent by job name. Over HTTP (as the hermes user) so the
   cron DB stays correctly owned.

2. **Read `config.yaml`** — the file ServiceBay's `hermes` template's
   post-deploy wrote with the `model:` block. Read via `podman exec`
   into the hermes container (`/opt/data/config.yaml`): the host file is
   owned by the hermes user with mode 640, so a direct host-path open as
   `core` gets EACCES/ENOENT.

3. **Splice in our `mcp_servers:` block** with HA-MCP, ServiceBay-MCP, the
   gatekeeper room-MCP, and the Audiobookshelf shim (when ABS_API_KEY is
   set) entries (and the cloud-LLM audit-proxy once that package ships).
   Remote-MCP shape per the
   https://hermes-agent.nousresearch.com/docs/reference/mcp-config-reference:

     mcp_servers:
       <name>:
         url: "<url>"
         headers:
           Authorization: "Bearer <token>"

   Hand-rolled YAML — same approach as ServiceBay's hermes/post-deploy.py,
   no PyYAML dependency. Re-running the script replaces the existing
   mcp_servers block in-place (idempotent).

4. **Write the merged file back** via `podman exec -i` into the hermes
   container, so it lands owned by the hermes user (same reason as the
   read in step 2).

5. **POST `/api/services/hermes/action {action: "restart"}`** via
   `SB_API_URL` so Hermes picks up the new mcp_servers block on next
   start. Hermes reads config.yaml only at boot; a slash-command
   `/reload-mcp` exists but only via an active gateway session, which
   we don't have from a post-deploy.

Caveat
======

If ServiceBay's `hermes` template is re-deployed, its post-deploy
overwrites config.yaml with just the model block — losing our
mcp_servers block. Re-deploy `solbay` to restore it.

Variables (ServiceBay substitutes them)
=======================================

From our variables.json:
  HERMES_API_PORT, HERMES_API_KEY,
  SERVICEBAY_MCP_URL, SERVICEBAY_MCP_TOKEN,
  GATEKEEPER_MCP_URL, GATEKEEPER_MCP_TOKEN,
  ABS_MCP_URL, ABS_MCP_TOKEN, ABS_API_KEY

From the ServiceBay platform:
  DATA_DIR, SB_API_URL, HOST, SB_API_TOKEN
"""

from __future__ import annotations

import datetime
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

# A ServiceBay-minted MCP token looks like `sb_<8-hex-id>_<base32-ish-secret>`.
# Only this shape is accepted by ServiceBay's `/mcp` `verifyToken`; the random
# opaque SERVICEBAY_MCP_TOKEN fallback never matches → permanent 401. We use
# this both to refuse persisting a junk token and to self-heal a box that
# already has one (#126).
SB_MCP_TOKEN_RE = re.compile(r"^sb_[0-9a-f]{8}_[A-Z2-9]+$")


# ───── env ─────────────────────────────────────────────────────────────────

# Initialize globals with default values
DATA_DIR = "/mnt/data"
SB_API_URL = "http://127.0.0.1:3000"
SB_API_TOKEN = ""
HERMES_API_PORT = "8642"
HERMES_API_KEY = ""
HERMES_API_URL = f"http://127.0.0.1:{HERMES_API_PORT}"
SERVICEBAY_MCP_URL = ""
SERVICEBAY_MCP_TOKEN = ""
GATEKEEPER_MCP_URL = ""
GATEKEEPER_MCP_TOKEN = ""
ABS_MCP_URL = ""
ABS_MCP_TOKEN = ""
ABS_API_KEY = ""
READINESS_TIMEOUT_S = 120

# The host file <DATA_DIR>/hermes/config.yaml lives on a volume the hermes
# image chowns to its own user (UID 10000 → host UID 534287, mode 640), so a
# direct open() as `core` gets EACCES/ENOENT. We read+write it through the
# hermes container instead, where /opt/data is config.yaml's home.
HERMES_CONTAINER = "hermes-hermes"
CONTAINER_CONFIG_PATH = "/opt/data/config.yaml"


def init_env() -> None:
    global \
        DATA_DIR, \
        SB_API_URL, \
        SB_API_TOKEN, \
        HERMES_API_PORT, \
        HERMES_API_KEY, \
        HERMES_API_URL
    global \
        SERVICEBAY_MCP_URL, \
        SERVICEBAY_MCP_TOKEN, \
        GATEKEEPER_MCP_URL, \
        GATEKEEPER_MCP_TOKEN, \
        ABS_MCP_URL, \
        ABS_MCP_TOKEN, \
        ABS_API_KEY, \
        READINESS_TIMEOUT_S, \
        HERMES_CONTAINER

    DATA_DIR = os.environ.get("DATA_DIR", "/mnt/data")
    SB_API_URL = os.environ.get("SB_API_URL", "http://127.0.0.1:3000").rstrip("/")
    SB_API_TOKEN = os.environ.get("SB_API_TOKEN", "")

    HERMES_API_PORT = os.environ.get("HERMES_API_PORT", "8642")
    HERMES_API_KEY = os.environ.get("HERMES_API_KEY", "")
    HERMES_API_URL = f"http://127.0.0.1:{HERMES_API_PORT}"

    SERVICEBAY_MCP_URL = os.environ.get("SERVICEBAY_MCP_URL", "")
    SERVICEBAY_MCP_TOKEN = os.environ.get("SERVICEBAY_MCP_TOKEN", "")
    # The gatekeeper MCP server always listens on the deterministic in-pod
    # port (gatekeeper container MCP_PORT, hard-coded 10760 in template.yml).
    # Default the URL when the variable is absent — e.g. a reinstall that
    # reuses a saved manifest predating this variable won't apply its
    # variables.json default, and we still want gatekeeper-mcp registered.
    GATEKEEPER_MCP_URL = (
        os.environ.get("GATEKEEPER_MCP_URL", "") or "http://127.0.0.1:10760/mcp"
    )
    GATEKEEPER_MCP_TOKEN = os.environ.get("GATEKEEPER_MCP_TOKEN", "")
    # The abs-mcp shim also listens on a deterministic in-pod port
    # (MCP_PORT 10770 in template.yml); default the URL for the same
    # saved-manifest-reinstall reason as gatekeeper-mcp above. ABS_API_KEY
    # gates registration — a keyless shim 401s on every ABS call, so
    # there's no point registering it until the key is set.
    ABS_MCP_URL = os.environ.get("ABS_MCP_URL", "") or "http://127.0.0.1:10770/mcp"
    ABS_MCP_TOKEN = os.environ.get("ABS_MCP_TOKEN", "")
    ABS_API_KEY = os.environ.get("ABS_API_KEY", "")

    READINESS_TIMEOUT_S = int(os.environ.get("HERMES_READINESS_TIMEOUT_S", "120"))
    HERMES_CONTAINER = os.environ.get("HERMES_CONTAINER", "hermes-hermes")


# ───── helpers ─────────────────────────────────────────────────────────────


def jlog(level: str, tag: str, message: str, **args: object) -> None:
    """Emit one JSON line on stdout matching ServiceBay's logger contract
    (docs/TEMPLATE_LOGGING.md): {ts, level, tag, message, args}."""
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


def hermes_get(path: str, timeout: float = 5.0) -> int:
    """GET against Hermes' API with bearer auth. Returns HTTP status, or 0
    for connection failure."""
    req = urllib.request.Request(f"{HERMES_API_URL}{path}", method="GET")
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
    """Call Hermes' API with bearer auth, returning (status, parsed-body).
    Status 0 on connection failure."""
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


def sb_post(path: str, payload: dict[str, object], timeout: float = 30.0) -> int:
    """POST against ServiceBay's API with the internal-token header (the
    same shape ServiceBay's own post-deploys use)."""
    status, _ = sb_post_json(path, payload, timeout)
    return status


def sb_post_json(
    path: str, payload: dict[str, object], timeout: float = 30.0
) -> tuple[int, dict[str, object] | None]:
    """Same as sb_post but also returns the parsed JSON body (or None
    when no body / non-JSON body)."""
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{SB_API_URL}{path}",
        data=body,
        method="POST",
    )
    req.add_header("Content-Type", "application/json")
    if SB_API_TOKEN:
        req.add_header("X-SB-Internal-Token", SB_API_TOKEN)
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
    """One mint attempt. Returns the minted secret, or None on any
    failure (non-200, missing/non-`sb_`-shaped secret)."""
    # Canonical route; `/api/system/mcp-tokens` is only an alias on newer
    # ServiceBay and 404s on some versions (#126).
    status, body = sb_post_json(
        "/api/system/api-tokens",
        # `read` is enough for the audit-query / status skills (they
        # read logs + service health); `mutate` and `lifecycle` cover
        # the future MCP-driven self-heal flows in the household stack.
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
    """Mint a real MCP token from ServiceBay's tokens API, so the value
    we splice into hermes' config.yaml is one ServiceBay actually
    recognises. Without this, the random SERVICEBAY_MCP_TOKEN that
    `assemble` generates is never registered and Hermes gets
    401 Unauthorized on every `/mcp` call (observed 2026-05-25).

    Retries a few times with a short backoff: the usual cause of a
    transient `None` is the SB-on-loopback readiness race (the post-deploy
    can fire before ServiceBay's own API answers on 127.0.0.1). Returns
    the minted secret on success, or None when every attempt failed — the
    caller must NOT persist a fallback (a non-`sb_` token is a permanent
    401; #126).
    """
    for attempt in range(1, attempts + 1):
        secret = _mint_servicebay_mcp_token_once()
        if secret:
            jlog(
                "info",
                "solbay:mcp",
                "minted servicebay-mcp token",
                attempt=attempt,
            )
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
    """Live-validate a bearer against ServiceBay's `/mcp` with a JSON-RPC
    `initialize`. 200 = the token is registered and accepted; 401 = stale
    / junk. Connection failure returns True (don't re-mint on a transient
    loopback hiccup — shape already passed)."""
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


# ───── config.yaml merge ───────────────────────────────────────────────────


def read_config_via_container() -> str | None:
    """Read /opt/data/config.yaml from inside the hermes container.

    The host file is owned by the hermes user (mode 640), unreadable to
    `core`; reading it through `podman exec` runs as the container user,
    which owns it. Returns the content, or None when the file is absent
    or the exec fails (caller treats that as "no config to merge into").
    """
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
    """Write config.yaml inside the hermes container via `podman exec -i`.

    Piped on stdin to `cat > <path>` so the file lands owned by the
    hermes user with its existing perms. Returns True on success."""
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
    """Remove an existing `mcp_servers:` top-level block from the file.

    Top-level = line starts at column 0 with `mcp_servers:` (any trailing
    whitespace OK). Block ends at the next column-0 non-comment non-blank
    line (the next top-level key), or EOF.

    Idempotent for the empty case: input without an mcp_servers block
    returns unchanged.
    """
    lines = content.splitlines(keepends=True)
    out: list[str] = []
    in_block = False
    for line in lines:
        if not in_block:
            stripped = line.lstrip()
            # Match `mcp_servers:` at column 0 (no leading whitespace).
            if line[:1] not in (" ", "\t") and stripped.startswith("mcp_servers:"):
                in_block = True
                continue  # drop this line
            out.append(line)
        else:
            # In block: end on the next column-0 non-comment non-blank line.
            stripped = line.lstrip()
            if (
                line[:1] not in (" ", "\t")
                and stripped
                and not stripped.startswith("#")
            ):
                in_block = False
                out.append(line)
            # else: still inside the block (indented, blank, or comment) → drop
    return "".join(out)


def existing_servicebay_mcp_token() -> str | None:
    """Pull the current `servicebay-mcp` bearer out of the live config.yaml
    (read through the hermes container). Returns the token only when it's
    present AND `sb_`-shaped — a junk/non-`sb_` value reads as None so the
    caller won't treat it as worth preserving. Used to avoid re-minting (or
    dropping) a token that's already valid when a redeploy's mint races SB
    readiness (#126)."""
    content = read_config_via_container()
    if not content:
        return None
    in_sb = False
    for line in content.splitlines():
        stripped = line.strip()
        if line[:1] not in (" ", "\t"):
            in_sb = False  # left the mcp_servers block
        if stripped.startswith("servicebay-mcp:"):
            in_sb = True
            continue
        if in_sb and stripped.startswith("Authorization:"):
            m = re.search(r"Bearer\s+(\S+)", stripped)
            token = m.group(1).strip('"') if m else ""
            return token if SB_MCP_TOKEN_RE.match(token) else None
    return None


def render_mcp_block(servers: list[tuple[str, str, str]]) -> str:
    """Render an `mcp_servers:` block for the given (name, url, token) entries.

    Remote-MCP shape per the Hermes MCP Config Reference; static-bearer
    auth is conveyed via the `headers` field and works without an explicit
    `auth:` declaration (the `auth: oauth` form in the reference is
    specific to OAuth flows).

    A token-less server (e.g. the pod-internal gatekeeper-mcp) gets NO
    `headers:` block — emitting `Authorization: "Bearer "` makes Hermes'
    httpx client reject it with "Illegal header value" and the MCP server
    never connects (observed live with gatekeeper-mcp).
    """
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
    """Read config.yaml, strip any existing mcp_servers block, append the
    rendered one. Returns True on write, False if config.yaml doesn't
    exist yet or the write failed (caller decides whether that's fatal).

    Read+write go through the hermes container (its user owns the file,
    mode 640 — `core` can't open it on the host)."""
    existing = read_config_via_container()
    if existing is None:
        return False
    stripped = strip_mcp_servers_block(existing)
    block = render_mcp_block(servers)
    # Append separator if there's preceding content
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


# ───── steps ───────────────────────────────────────────────────────────────


def wait_for_hermes() -> None:
    deadline = time.time() + READINESS_TIMEOUT_S
    last_status: int | None = None
    while time.time() < deadline:
        status = hermes_get("/health")
        # 200 = healthy. 401/403 = auth wall but listener up — sufficient
        # for "container ready" (we won't be able to call further routes
        # if our token is wrong, but the post-deploy will surface that
        # via the restart-API call instead).
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


def restart_hermes_via_sb_api() -> bool:
    status = sb_post("/api/services/hermes/action", {"action": "restart"})
    if status == 200:
        jlog(
            "info",
            "solbay:restart",
            "hermes restart requested via ServiceBay API",
        )
        return True
    jlog(
        "warn",
        "solbay:restart",
        "restart request failed; new mcp_servers block lands on next manual restart",
        status=status,
    )
    return False


def collect_mcp_servers() -> list[tuple[str, str, str]]:
    """Pair each MCP with its token; skip empty entries."""
    servers: list[tuple[str, str, str]] = []
    # ha-mcp intentionally not wired: Home Assistant is served by Hermes'
    # native `homeassistant` toolset (ha_call_service / ha_get_state / ...).
    # HA's own /mcp_server/sse endpoint was redundant and flaky (Session
    # terminated), so it is not added. (HA_MCP_* template vars removed.)
    if SERVICEBAY_MCP_URL:
        # SERVICEBAY_MCP_TOKEN from `assemble` is a random value that nothing
        # registered against ServiceBay's mcp-tokens table — splicing it in
        # yields a permanent 401 (#126). Self-heal: if config.yaml already
        # carries a VALID (`sb_`-shaped, live-probed 200) servicebay-mcp
        # token, keep it; otherwise mint a fresh one. Only when both fail do
        # we SKIP the entry — never persist the junk SERVICEBAY_MCP_TOKEN
        # fallback. A missing entry is more diagnosable than a silent 401.
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
        jlog(
            "info",
            "solbay:mcp",
            "servicebay-mcp skipped",
            reason="missing url",
        )
    if GATEKEEPER_MCP_URL:
        # The gatekeeper's room MCP server runs in-pod with no enable flag,
        # so register it whenever the URL is set. An empty token is valid
        # (pod-internal listener, open — same as PUSH_TOKEN blank).
        servers.append(("gatekeeper-mcp", GATEKEEPER_MCP_URL, GATEKEEPER_MCP_TOKEN))
    else:
        jlog(
            "info",
            "solbay:mcp",
            "gatekeeper-mcp skipped",
            reason="missing url",
        )
    if ABS_API_KEY:
        # Only register the Audiobookshelf shim once it has a credential —
        # without ABS_API_KEY every tool call returns abs_unavailable, so a
        # keyless registration is just dead surface in Hermes.
        servers.append(("abs-mcp", ABS_MCP_URL, ABS_MCP_TOKEN))
    else:
        jlog(
            "info",
            "solbay:mcp",
            "abs-mcp skipped",
            reason="no ABS_API_KEY (shim runs inert until set)",
        )
    return servers


CHRONICLE_JOB_NAME = "sol-daily-chronicle"
PROBLEM_SUMMARIZER_JOB_NAME = "sol-problem-summarizer"


def _register_cron(name: str, schedule: str, prompt: str) -> None:
    """Register a Hermes cron job by name, idempotently. Skips when a job of
    the same name already exists.

    Registered over HTTP (not `hermes cron create` via podman) so the write
    goes through the gateway as the hermes user, keeping
    /opt/data/cron/jobs.json owned correctly; a root-written jobs.json breaks
    the gateway's own cron reads.
    """
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
        jlog(
            "info",
            "solbay:cron",
            "registered cron",
            name=name,
            schedule=schedule,
        )
    else:
        jlog(
            "warn",
            "solbay:cron",
            "cron registration failed",
            name=name,
            status=create_status,
        )


def register_chronicle_cron() -> None:
    """Register the daily family-chronicle cron job via Hermes' jobs API
    (#83)."""
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
    """Register the weekly troubleshooting-KB cron job via Hermes' jobs API
    (#182)."""
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


def main() -> int:
    init_env()
    wait_for_hermes()
    register_chronicle_cron()
    register_problem_summarizer_cron()
    servers = collect_mcp_servers()
    if not merge_config_yaml(servers):
        return 0  # config.yaml doesn't exist; nothing to do, not fatal
    if servers:
        restart_hermes_via_sb_api()
    jlog("info", "solbay:post-deploy", "done", mcp_count=len(servers))
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Post-deploy hook for the `admin-soul` template.

Wires the operator/admin soul into the shared Hermes. There is one Hermes
instance; the admin soul is realised as its skill pack (#176) plus a
*distinct, full-admin* ServiceBay-MCP entry in Hermes' config.yaml.

What it does (idempotent on every run)
======================================

1. **Wait for Hermes** to be reachable on its API port.

2. **Mint a ServiceBay-MCP token** with scopes
   ``["read", "lifecycle", "mutate"]`` via ``POST /api/system/api-tokens``
   — the household soul gets read+lifecycle; the operator soul additionally
   gets ``mutate`` so it can start/stop/restart/redeploy services and patch
   config/proxy-routes. **No ``destroy`` scope is granted** — irreversible
   actions are gated behind the admin-act skill's human-confirmation rule
   (#176), not handed to the LLM autonomously.

3. **Splice a ``servicebay_admin`` entry** into the shared Hermes
   config.yaml's ``mcp_servers:`` block, leaving every other entry (the
   household ``servicebay-mcp`` / ``ha-mcp`` / ``gatekeeper-mcp``)
   untouched. Self-heal (#126): a present ``servicebay_admin`` entry with a
   valid (``sb_``-shaped, live-probe 200) bearer is left as-is; a junk/stale
   one is re-minted and rewritten in place. A non-``sb_`` token is a permanent
   401, so we NEVER persist a fallback — if minting fails we skip the entry
   (a missing entry is more diagnosable than a silently-401 one).

4. **Restart Hermes** via the ServiceBay API so it re-reads config.yaml
   (Hermes reads MCP config only at boot).

The host file ``<DATA_DIR>/hermes/config.yaml`` is owned by the hermes user
(mode 640), unreadable to ``core``; we read/write it through the
``hermes-hermes`` container, same as templates/solbay/post-deploy.py.
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

# A ServiceBay-minted MCP token is `sb_<8-hex-id>_<base32-ish-secret>`. Only
# this shape is accepted by ServiceBay's `/mcp` verifyToken; any other value is
# a permanent 401 (#126). Used to refuse a junk token and to self-heal a box
# that already wrote one.
SB_MCP_TOKEN_RE = re.compile(r"^sb_[0-9a-f]{8}_[A-Z2-9]+$")

# The admin soul's distinct MCP entry name. Kept separate from the household
# `servicebay-mcp` entry so the two souls' scopes never get conflated and
# either can be re-minted without disturbing the other.
ADMIN_MCP_NAME = "servicebay_admin"
# Full-admin scopes the operator soul gets. `mutate` enables start/stop/
# restart/redeploy/config; `destroy` is deliberately excluded (#175 guardrail).
ADMIN_MCP_SCOPES = ["read", "lifecycle", "mutate"]
ADMIN_TOKEN_NAME = "admin-soul"

# Initialised in init_env().
DATA_DIR = "/mnt/data"
SB_API_URL = "http://127.0.0.1:3000"
SB_API_TOKEN = ""
HERMES_API_PORT = "8642"
HERMES_API_KEY = ""
HERMES_API_URL = f"http://127.0.0.1:{HERMES_API_PORT}"
SERVICEBAY_MCP_URL = "http://127.0.0.1:5888/mcp"
READINESS_TIMEOUT_S = 120

# The host config.yaml is owned by the hermes user (mode 640); read+write it
# through the hermes container, where /opt/data is its home.
HERMES_CONTAINER = "hermes-hermes"
CONTAINER_CONFIG_PATH = "/opt/data/config.yaml"


def init_env() -> None:
    global DATA_DIR, SB_API_URL, SB_API_TOKEN
    global HERMES_API_PORT, HERMES_API_KEY, HERMES_API_URL
    global SERVICEBAY_MCP_URL, READINESS_TIMEOUT_S, HERMES_CONTAINER

    DATA_DIR = os.environ.get("DATA_DIR", "/mnt/data")
    SB_API_URL = os.environ.get("SB_API_URL", "http://127.0.0.1:3000").rstrip("/")
    SB_API_TOKEN = os.environ.get("SB_API_TOKEN", "")
    HERMES_API_PORT = os.environ.get("HERMES_API_PORT", "8642")
    HERMES_API_KEY = os.environ.get("HERMES_API_KEY", "")
    HERMES_API_URL = f"http://127.0.0.1:{HERMES_API_PORT}"
    SERVICEBAY_MCP_URL = (
        os.environ.get("SERVICEBAY_MCP_URL", "") or "http://127.0.0.1:5888/mcp"
    )
    READINESS_TIMEOUT_S = int(os.environ.get("HERMES_READINESS_TIMEOUT_S", "120"))
    HERMES_CONTAINER = os.environ.get("HERMES_CONTAINER", "hermes-hermes")


# ───── helpers ─────────────────────────────────────────────────────────────


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


def hermes_get(path: str, timeout: float = 5.0) -> int:
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


def sb_post_json(
    path: str, payload: dict[str, object], timeout: float = 30.0
) -> tuple[int, dict[str, object] | None]:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(f"{SB_API_URL}{path}", data=body, method="POST")
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


def _mint_admin_token_once() -> str | None:
    """One mint attempt for the full-admin token. Returns the `sb_`-shaped
    secret, or None on any failure."""
    # Canonical route; `/api/system/mcp-tokens` is only an alias on newer
    # ServiceBay and 404s on some versions (#126).
    status, body = sb_post_json(
        "/api/system/api-tokens",
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
    """Mint a read+lifecycle+mutate ServiceBay-MCP token for the operator
    soul. Retries a few times for the SB-on-loopback readiness race (#126).
    Returns the minted `sb_`-shaped secret, or None when every attempt
    failed — the caller must NOT persist any non-`sb_` fallback."""
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
    """Live-validate a bearer against ServiceBay's `/mcp` with a JSON-RPC
    `initialize`. 200 = registered + accepted; 401 = stale/junk. A connection
    failure returns True so a transient loopback hiccup doesn't trigger a
    needless re-mint when the shape already passed (#126)."""
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


# ───── config.yaml read/write (through the hermes container) ─────────────────


def read_config_via_container() -> str | None:
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
            "admin-soul:config",
            "could not exec into hermes container to read config.yaml",
            container=HERMES_CONTAINER,
            error=str(e),
        )
        return None
    if proc.returncode != 0:
        jlog(
            "warn",
            "admin-soul:config",
            "config.yaml not found",
            path=f"{HERMES_CONTAINER}:{CONTAINER_CONFIG_PATH}",
            stderr=proc.stderr.strip(),
        )
        return None
    return proc.stdout


def write_config_via_container(content: str) -> bool:
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
            "admin-soul:config",
            "could not exec into hermes container to write config.yaml",
            container=HERMES_CONTAINER,
            error=str(e),
        )
        return False
    if proc.returncode != 0:
        jlog(
            "error",
            "admin-soul:config",
            "writing config.yaml in hermes container failed",
            container=HERMES_CONTAINER,
            stderr=proc.stderr.strip(),
        )
        return False
    return True


def extract_mcp_block(content: str) -> str:
    """Return the top-level `mcp_servers:` block (header + indented body), or
    '' if absent. The block ends at the next top-level key or EOF."""
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
    """Pull the `servicebay_admin` entry's bearer from an mcp_servers block.
    Returns the token (any shape) or None when the entry is absent."""
    in_entry = False
    for line in mcp_block.splitlines():
        stripped = line.strip()
        # Entry keys sit at 2-space indent and end with `:`.
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
    preserving every other entry. The sub-block runs from its 2-space-indented
    key to the next entry key / dedent or EOF."""
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
            # else: still inside the stale entry → drop
            continue
        out.append(line)
    return "".join(out)


def ensure_admin_mcp_entry() -> bool:
    """Ensure config.yaml carries a `mcp_servers.servicebay_admin` entry with a
    VALID full-admin bearer, leaving every other MCP entry untouched. Mints a
    fresh token and writes the entry in place.

    Self-heal (#126): a present entry whose bearer is `sb_`-shaped AND live-
    probes 200 is left as-is (idempotent no-op). A junk/stale one is re-minted
    and rewritten. Mint failure → leave the file untouched, skip the entry
    (never persist a non-`sb_` token).

    Returns True when config.yaml was mutated (signals the caller to restart
    Hermes), False otherwise.
    """
    existing = read_config_via_container()
    if existing is None:
        # config.yaml absent — the hermes template hasn't written it yet.
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


# ───── steps ───────────────────────────────────────────────────────────────


def wait_for_hermes() -> None:
    deadline = time.time() + READINESS_TIMEOUT_S
    last_status: int | None = None
    while time.time() < deadline:
        status = hermes_get("/health")
        if status in (200, 401, 403):
            jlog("info", "admin-soul:hermes", "ready", status=status)
            return
        last_status = status
        time.sleep(2)
    jlog(
        "error",
        "admin-soul:hermes",
        "Hermes not reachable within readiness window",
        last_status=last_status,
        timeout_s=READINESS_TIMEOUT_S,
    )
    raise SystemExit(1)


def restart_hermes_via_sb_api() -> bool:
    status, _ = sb_post_json("/api/services/hermes/action", {"action": "restart"})
    if status == 200:
        jlog(
            "info", "admin-soul:restart", "hermes restart requested via ServiceBay API"
        )
        return True
    jlog(
        "warn",
        "admin-soul:restart",
        "restart request failed; servicebay_admin entry lands on next manual restart",
        status=status,
    )
    return False


def main() -> int:
    init_env()
    wait_for_hermes()
    if ensure_admin_mcp_entry():
        restart_hermes_via_sb_api()
    jlog("info", "admin-soul:post-deploy", "done")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""
post-deploy hook for the `hermes-webui` template.

One responsibility today: **decommission the orphaned `open-webui` pod
when present** (#1083). PR #1054 replaced `templates/open-webui/` with
`templates/hermes-webui/` at the same `chat.<publicDomain>/` URL, but
removing the template directory in source doesn't remove the pod from
boxes that had it installed. Without this hook, an upgrade leaves:

  - The `open-webui` pod still running, eating RAM/CPU.
  - `${DATA_DIR}/open-webui/` (its SQLite + uploads) on disk, with no
    way back into a UI now that the template's gone.
  - The NPM `chat.<publicDomain>` proxy still pointed at the old port
    until hermes-webui's wizard-side NPM registration overrides it.
  - `/services` silently dropping the open-webui row because SB can't
    find a template called `open-webui` to render anymore.

We can't put this migration inside `templates/open-webui/migrations/`
the way `templates/CLAUDE.md` describes — the template directory
itself is gone. Hosting it in hermes-webui's post-deploy is the
closest fit: it runs on every hermes-webui (re)install, and a fresh
install detects-no-orphan and does nothing.

Steps:

  1. Detect via `GET /api/settings` → `installedTemplates.open-webui`.
     Fresh install or already-migrated boxes: no-op.
  2. Archive `${DATA_DIR}/open-webui/` to
     `${DATA_DIR}/_archived/open-webui-<YYYY-MM-DD-HHMMSS>/` so the
     operator can rescue chat history if anyone was using it. Best-
     effort: a permission failure here is non-fatal.
  3. `DELETE /api/services/open-webui` — stops the pod + trashes the
     `.kube`/`.yml` via the existing `ServiceManager.deleteService`
     path (which is what the UI's per-service Delete button uses).
  4. POST `/api/settings` with the updated `installedTemplates` map
     so SB stops thinking open-webui is installed.

What we DON'T do here:

  - Explicit NPM proxy re-target. hermes-webui's wizard-side
    subdomain registration claims `chat.<publicDomain>` via the
    same upsert path the original Open WebUI install used; the
    proxy is overwritten by hermes-webui's normal install machinery.
    If a future regression in that upsert breaks the swap, file a
    bug — DO NOT plumb a second proxy-update path in here.
  - Per-user Open WebUI chat-history migration into hermes-webui.
    We checked: hermes-webui talks to Hermes directly, sessions
    are stored under `~/.hermes/sessions/`, not migrated from
    Open WebUI's separate SQLite. Archived chats are an offline
    rescue, not a live import.
"""

from __future__ import annotations

import datetime
import json
import os
import sys
import urllib.error
import urllib.request


OPEN_WEBUI_NAME = "open-webui"


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
        jlog("warn", "hermes-webui:decom", "HTTP error", url=url, error=str(e))
        return 0, None


def get_installed_templates(sb_api: str) -> dict[str, object] | None:
    status, body = http_request(f"{sb_api}/api/settings")
    if status != 200 or not isinstance(body, dict):
        return None
    installed = body.get("installedTemplates")
    return installed if isinstance(installed, dict) else None


def archive_open_webui_data(data_dir: str) -> str | None:
    src = os.path.join(data_dir, OPEN_WEBUI_NAME)
    if not os.path.isdir(src):
        return None
    stamp = datetime.datetime.now().strftime("%Y-%m-%d-%H%M%S")
    archive_root = os.path.join(data_dir, "_archived")
    dst = os.path.join(archive_root, f"{OPEN_WEBUI_NAME}-{stamp}")
    try:
        os.makedirs(archive_root, exist_ok=True)
        os.rename(src, dst)
    except OSError as e:
        jlog(
            "warn",
            "hermes-webui:decom",
            "could not archive open-webui data dir; data left in place for manual cleanup",
            src=src,
            error=str(e),
        )
        return None
    jlog("info", "hermes-webui:decom", "archived open-webui data dir", src=src, dst=dst)
    return dst


def delete_open_webui_service(sb_api: str) -> bool:
    status, _ = http_request(
        f"{sb_api}/api/services/{OPEN_WEBUI_NAME}",
        method="DELETE",
        timeout=30,
    )
    if status == 200:
        jlog("info", "hermes-webui:decom", "deleted open-webui service via SB API")
        return True
    jlog(
        "warn",
        "hermes-webui:decom",
        "could not delete open-webui via SB API — operator may need to remove the pod manually",
        status=status,
    )
    return False


def remove_from_installed_templates(sb_api: str, installed: dict[str, object]) -> bool:
    if OPEN_WEBUI_NAME not in installed:
        return True
    pruned = {k: v for k, v in installed.items() if k != OPEN_WEBUI_NAME}
    status, _ = http_request(
        f"{sb_api}/api/settings",
        method="POST",
        payload={"installedTemplates": pruned},
        timeout=15,
    )
    if status == 200:
        jlog("info", "hermes-webui:decom", "removed open-webui from installedTemplates")
        return True
    jlog(
        "warn",
        "hermes-webui:decom",
        "could not update installedTemplates — SB will keep showing open-webui as installed until the next config edit",
        status=status,
    )
    return False


def decommission_open_webui(sb_api: str, data_dir: str) -> None:
    installed = get_installed_templates(sb_api)
    if installed is None:
        jlog(
            "warn",
            "hermes-webui:decom",
            "could not read installedTemplates; skipping decommission check",
        )
        return
    if OPEN_WEBUI_NAME not in installed:
        return  # Fresh install or already-decommissioned — no-op
    jlog(
        "info",
        "hermes-webui:decom",
        "open-webui detected as installed — beginning decommission for #1083",
    )
    archive_open_webui_data(data_dir)
    delete_open_webui_service(sb_api)
    remove_from_installed_templates(sb_api, installed)
    jlog("info", "hermes-webui:decom", "open-webui decommission complete")


def main() -> int:
    sb_api = env("SB_API_URL", "http://localhost:3000")
    data_dir = env("DATA_DIR", "/mnt/data/stacks")
    decommission_open_webui(sb_api, data_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())

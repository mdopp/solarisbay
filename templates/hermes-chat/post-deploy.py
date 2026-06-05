#!/usr/bin/env python3
"""post-deploy hook for the `hermes-chat` template.

Decommission the chat pods this one replaces. `hermes-chat` (#140) takes
over `chat.<publicDomain>` from the in-process `hermes-webui` (#139), which
itself replaced `open-webui` (#1044). Removing a template directory in
source doesn't remove an already-installed pod from a box, so we tear down
both predecessors here when present (idempotent — a fresh install finds
neither and no-ops):

  - Stop the pod (`DELETE /api/services/<name>`) so it stops eating
    RAM/CPU and releases the `chat.<publicDomain>` proxy.
  - Archive its data dir to `${DATA_DIR}/_archived/<name>-<stamp>/` so an
    operator can rescue anything. Best-effort; a permission failure is
    non-fatal. (hermes-webui kept no own store under DATA_DIR; open-webui
    did — its SQLite + uploads.)
  - Drop it from `installedTemplates` so `/services` stops rendering a row
    for a template that no longer exists.

We don't re-target the NPM proxy here: hermes-chat's wizard-side subdomain
registration claims `chat.<publicDomain>` via the same upsert the
predecessors used.
"""

from __future__ import annotations

import datetime
import json
import os
import sys
import urllib.error
import urllib.request


# Ordered oldest → newest so an archive of open-webui lands before any
# hermes-webui teardown that shares no data.
RETIRED_NAMES = ("open-webui", "hermes-webui")


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
        jlog("warn", "hermes-chat:decom", "HTTP error", url=url, error=str(e))
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
            "hermes-chat:decom",
            "could not archive data dir; left in place for manual cleanup",
            src=src,
            error=str(e),
        )
        return None
    jlog("info", "hermes-chat:decom", "archived data dir", src=src, dst=dst)
    return dst


def delete_service(sb_api: str, name: str) -> bool:
    status, _ = http_request(
        f"{sb_api}/api/services/{name}",
        method="DELETE",
        timeout=30,
    )
    if status == 200:
        jlog("info", "hermes-chat:decom", "deleted service via SB API", service=name)
        return True
    jlog(
        "warn",
        "hermes-chat:decom",
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
            "hermes-chat:decom",
            "removed retired templates from installedTemplates",
            removed=to_prune,
        )
        return
    jlog(
        "warn",
        "hermes-chat:decom",
        "could not update installedTemplates — SB will keep showing them as installed until the next config edit",
        status=status,
    )


def decommission(sb_api: str, data_dir: str) -> None:
    installed = get_installed_templates(sb_api)
    if installed is None:
        jlog(
            "warn",
            "hermes-chat:decom",
            "could not read installedTemplates; skipping decommission check",
        )
        return
    present = [name for name in RETIRED_NAMES if name in installed]
    if not present:
        return  # Fresh install or already-decommissioned — no-op
    jlog(
        "info",
        "hermes-chat:decom",
        "retired chat pods detected — beginning decommission for #139/#140",
        present=present,
    )
    for name in present:
        archive_data_dir(data_dir, name)
        delete_service(sb_api, name)
    remove_from_installed_templates(sb_api, installed, present)
    jlog("info", "hermes-chat:decom", "decommission complete", removed=present)


def main() -> int:
    sb_api = env("SB_API_URL", "http://localhost:3000")
    data_dir = env("DATA_DIR", "/mnt/data/stacks")
    decommission(sb_api, data_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())

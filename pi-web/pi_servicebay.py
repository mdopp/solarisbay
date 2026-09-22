#!/usr/bin/env python3
"""`servicebay` — ServiceBay's agent CLI on `$PATH`, with the right token.

The CLI itself is ServiceBay's (`agent-cli/servicebay.mjs`, servicebay#2906),
delivered to the box and mounted read-only here. It is dependency-free JS, so
running it is `node <path> <verb>` — and it deliberately has **no `--token`
flag**: it reads the token from the file named by `SERVICEBAY_MCP_TOKEN_FILE`,
because `/proc/<pid>/cmdline` is world-readable.

This wrapper is the two things the CLI cannot know by itself:

  WHERE IT IS — `$SERVICEBAY_AGENT_KIT/agent-cli/servicebay.mjs`, so a session
    types `servicebay services` instead of a path.

  WHOSE TOKEN IT USES — a PI WEB session's cwd is the project it works in, and
    a project added with `pi-web-project add` has its own delegated read-only
    token (#1395). That one is used when the cwd is inside such a project;
    otherwise the pod's own token. Both are 0600 files under
    `/data/servicebay`, and the path is all that is passed on — the secret
    reaches neither argv nor this process's environment.
"""

from __future__ import annotations

import os
import sys

DEFAULT_KIT = "/opt/servicebay"
DEFAULT_WORKSPACE = "/workspace"
DEFAULT_ENTRY_DIR = "/data/servicebay/projects"
DEFAULT_PARENT_TOKEN_FILE = "/data/servicebay/parent-token"

TOKEN_FILE_ENV = "SERVICEBAY_MCP_TOKEN_FILE"
TOKEN_ENV = "SERVICEBAY_MCP_TOKEN"


def config_from_env(env=os.environ) -> dict:
    return {
        "kit": env.get("SERVICEBAY_AGENT_KIT") or DEFAULT_KIT,
        "workspace": env.get("PI_WEB_WORKSPACE") or DEFAULT_WORKSPACE,
        "entry_dir": env.get("PI_WEB_PROJECT_DIR") or DEFAULT_ENTRY_DIR,
        "parent_token_file": (
            env.get("PI_WEB_SB_TOKEN_FILE") or DEFAULT_PARENT_TOKEN_FILE
        ),
        "project": env.get("PI_WEB_PROJECT", ""),
    }


def cli_path(kit: str) -> str:
    return os.path.join(kit, "agent-cli", "servicebay.mjs")


def project_for_cwd(workspace: str, cwd: str) -> str:
    """The project folder the cwd sits in, or `""` outside the workspace.

    Only the first segment under the workspace counts, so a session deep inside
    a repository still reads the box as that project.
    """
    root = os.path.normpath(workspace)
    path = os.path.normpath(cwd)
    if path == root or not path.startswith(root + os.sep):
        return ""
    return path[len(root) + 1 :].split(os.sep)[0]


def project_token_file(entry_dir: str, project: str) -> str:
    return os.path.join(entry_dir, f"{project}.token")


def token_file(cfg: dict, cwd: str, exists=os.path.exists) -> str:
    """The token file this invocation authenticates with.

    Falling back to the pod's token is on purpose: a hand-cloned checkout that
    was never `add`ed still gets read access to the box, at the pod's own
    read-only scope, rather than an error a session cannot act on.
    """
    project = cfg["project"] or project_for_cwd(cfg["workspace"], cwd)
    if project:
        path = project_token_file(cfg["entry_dir"], project)
        if exists(path):
            return path
    return cfg["parent_token_file"]


def _usable(path: str) -> bool:
    """A token file is usable when it exists and carries something."""
    try:
        with open(path, "rb") as handle:
            return bool(handle.read(1))
    except OSError:
        return False


def explicit_token_file(env, usable=_usable) -> tuple[str, str]:
    """What an explicitly named token file resolves to.

    Returns `(path, "")` when it is usable, `("", reason)` when it was named
    but is unusable, and `("", "")` when nothing was named.

    Naming a file is an instruction, not a hint: the pod's own token is
    broader than any delegated one a caller would bother to name, so falling
    back to it on a typo hands out `mutate` where `read` was asked for. That
    happened — a check meant to prove a read-only refusal ran a real
    force-update instead (#1461).
    """
    path = env.get(TOKEN_FILE_ENV, "")
    if not path:
        return "", ""
    if usable(path):
        return path, ""
    return "", (
        f"servicebay: {TOKEN_FILE_ENV} names {path}, which is missing, "
        "unreadable or empty. Refusing to fall back to this container's own "
        "token — it is wider than the one you named. Fix the file or unset "
        "the variable."
    )


def child_env(env, path: str) -> dict:
    """The CLI's environment: the token *file*, and no token value anywhere."""
    child = dict(env)
    child[TOKEN_FILE_ENV] = path
    child.pop(TOKEN_ENV, None)
    return child


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    cfg = config_from_env()
    script = cli_path(cfg["kit"])
    if not os.path.exists(script):
        print(
            f"servicebay: the ServiceBay agent kit is not mounted at {cfg['kit']} "
            f"({script} is missing), so there is no CLI to run. The pod mounts it "
            "read-only from the box; if this is a fresh install, the box's "
            "ServiceBay may be older than the release that delivers it.",
            file=sys.stderr,
        )
        return 1
    named, problem = explicit_token_file(os.environ)
    if problem:
        print(problem, file=sys.stderr)
        return 2
    env = child_env(os.environ, named or token_file(cfg, os.getcwd()))
    os.execvpe("node", ["node", script, *args], env)


if __name__ == "__main__":
    sys.exit(main())

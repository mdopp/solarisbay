#!/usr/bin/env python3
"""`gh` on `$PATH`, holding the token this pod already has.

The GitHub CLI is installed at GH_REAL below; this wrapper is the one thing it
cannot do for itself here -- find its credential. `gh auth login --with-token`
is not the way in: it validates the token against the `read:org` scope, which a
token minted for pushing code has no reason to carry. `GH_TOKEN` skips that
check and is what gh reads anyway.

The token comes from the same mode-0600 store git uses
(`/data/pi-web/git-credentials`, written by the `pi-web-git-credentials` init
container) and is passed to the child through its environment only. It is never
an argument -- `/proc/<pid>/cmdline` is world-readable and this container has
real user logins on it -- and never reaches stdout.

One-for-one with `pi_servicebay.py`, which solves the same problem for the
ServiceBay CLI.
"""

import os
import sys
from urllib.parse import urlsplit

GH_REAL = "/usr/local/lib/gh/bin/gh"
STORE = os.environ.get("PI_WEB_GIT_CREDENTIALS", "/data/pi-web/git-credentials")


def token_from_store(path: str) -> str:
    """The password half of the first `https://user:token@host` line, or ''."""
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                password = urlsplit(line).password
                if password:
                    return password
    except OSError:
        return ""
    return ""


def main() -> int:
    if not os.path.exists(GH_REAL):
        sys.stderr.write(
            "gh: the GitHub CLI is not installed at %s. The image builds it in; "
            "if this is a fresh pod, its image predates that change.\n" % GH_REAL
        )
        return 127

    env = dict(os.environ)
    if not env.get("GH_TOKEN") and not env.get("GITHUB_TOKEN"):
        token = token_from_store(STORE)
        if token:
            env["GH_TOKEN"] = token
        # No token is not an error here: `gh --help` and `gh --version` work
        # without one, and gh's own message for the rest says more than ours.

    try:
        os.execve(GH_REAL, [GH_REAL] + sys.argv[1:], env)
    except OSError as exc:  # pragma: no cover - exec only fails if the file broke
        sys.stderr.write("gh: cannot run %s: %s\n" % (GH_REAL, exc))
        return 126


if __name__ == "__main__":
    raise SystemExit(main())

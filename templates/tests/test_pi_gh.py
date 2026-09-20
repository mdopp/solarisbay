"""The `gh` wrapper in the pi-web pod (#1439).

Everything asserted here has a wrong answer that looks like a working install:

* **The path the wrapper execs.** The Dockerfile puts the real binary somewhere
  and the wrapper execs a constant. Nothing checks they agree, and a mismatch
  looks perfect until a session runs `gh` and gets 127 -- on a box, in the
  middle of somebody's work.
* **A token that is silently absent.** `token_from_store` returning "" on a
  missing file is deliberate: `gh --version` must work without a credential.
  That makes an empty return the same shape as a broken store, so the reading
  itself has to be proven.
* **Not clobbering a token the caller set.** A session that exports GH_TOKEN for
  one command means it; the wrapper reading the store over it would send the
  wrong identity and still look like it worked.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

TEMPLATES = pathlib.Path(__file__).resolve().parents[1]
ROOT = TEMPLATES.parent
WRAPPER = ROOT / "pi-web" / "pi_gh.py"
DOCKERFILE = ROOT / "pi-web" / "Dockerfile"


@pytest.fixture(scope="module")
def gh():
    spec = importlib.util.spec_from_file_location("pi_gh", WRAPPER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["pi_gh"] = module
    spec.loader.exec_module(module)
    return module


def test_reads_the_password_half_of_the_store(gh, tmp_path):
    store = tmp_path / "git-credentials"
    store.write_text("https://x-access-token:ghp_secret@github.com\n", encoding="utf-8")
    assert gh.token_from_store(str(store)) == "ghp_secret"


def test_a_missing_store_is_empty_not_an_exception(gh, tmp_path):
    assert gh.token_from_store(str(tmp_path / "nope")) == ""


def test_blank_and_passwordless_lines_are_skipped(gh, tmp_path):
    store = tmp_path / "git-credentials"
    store.write_text(
        "\n\nhttps://github.com\nhttps://user:real@github.com\n", encoding="utf-8"
    )
    assert gh.token_from_store(str(store)) == "real"


def test_the_dockerfile_installs_gh_where_the_wrapper_execs_it(gh):
    """The wrapper's constant and the Dockerfile's install target are one
    contract split across two files. Nothing else notices when they drift."""
    assert gh.GH_REAL in DOCKERFILE.read_text(encoding="utf-8")


def test_the_wrapper_is_what_a_session_finds_as_gh(gh):
    dockerfile = DOCKERFILE.read_text(encoding="utf-8")
    assert "pi_gh.py /usr/local/bin/gh" in dockerfile

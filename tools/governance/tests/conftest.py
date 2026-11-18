"""Fixtures for the governance tests (helpers live in governance_testkit.py).

The suite runs with ``--import-mode=importlib``, so this file puts its own folder on
``sys.path`` for the helper module instead of relying on rootdir insertion.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from governance_testkit import Repo, git_env  # noqa: E402


@pytest.fixture
def repo(tmp_path: Path) -> Repo:
    """A repo on ``main`` with one clean commit, pushed to a bare local origin."""
    if shutil.which("git") is None:  # pragma: no cover - CI images ship git
        pytest.skip("git is not installed")
    home = tmp_path / "home"
    home.mkdir()
    env = git_env(home)
    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    subprocess.run(["git", "init", "--quiet", "--bare", "-b", "main", str(origin)], env=env, check=True)
    subprocess.run(["git", "init", "--quiet", "-b", "main", str(work)], env=env, check=True)
    r = Repo(path=work, origin=origin, env=env)
    r.git("config", "commit.gpgsign", "false")  # local test repo only; no key exists here
    r.git("remote", "add", "origin", str(origin))
    r.commit("chore: initial commit\n\nWhy: start.\n", rel="README.md", text="# test\n")
    r.push_main()
    return r

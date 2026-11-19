"""scripts/check_no_session_links.sh, run for real in throwaway git repositories."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "check_no_session_links.sh"

# Built from pieces so this file never contains the patterns it plants.
LINK = "https://claude" + ".ai/" + "code/session_0123"
TRAILER = "Claude-" + "Session: abc"

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


def git_env(home: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(
        HOME=str(home),
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
        GIT_AUTHOR_NAME="Test",
        GIT_AUTHOR_EMAIL="test@example.invalid",
        GIT_COMMITTER_NAME="Test",
        GIT_COMMITTER_EMAIL="test@example.invalid",
    )
    return env


class Repo:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.env = git_env(path)
        self.git("init", "-q", "-b", "main")
        self.commit_file("README.md", "clean\n", "chore: start")
        self.git("update-ref", "refs/remotes/origin/main", "HEAD")

    def git(self, *args: str) -> str:
        done = subprocess.run(
            ["git", *args], cwd=self.path, env=self.env, check=True, capture_output=True, text=True
        )
        return done.stdout

    def commit_file(self, name: str, text: str, message: str) -> None:
        (self.path / name).write_text(text, encoding="utf-8")
        self.git("add", name)
        self.git("commit", "-q", "-m", message)

    def check(self, *args: str, base: str | None = None) -> subprocess.CompletedProcess[str]:
        env = dict(self.env)
        if base is not None:
            env["BASE"] = base
        return subprocess.run(
            ["bash", str(SCRIPT), *args], cwd=self.path, env=env, capture_output=True, text=True
        )


@pytest.fixture
def repo(tmp_path: Path) -> Repo:
    return Repo(tmp_path)


def test_clean_repository_passes(repo: Repo) -> None:
    repo.commit_file("notes.md", "nothing to see\n", "docs: notes")
    result = repo.check("--require-base")
    assert (result.returncode, result.stdout) == (0, "")


def test_link_in_a_tracked_file_fails_and_names_the_file(repo: Repo) -> None:
    repo.commit_file("notes.md", f"see {LINK}\n", "docs: notes")
    result = repo.check()
    assert result.returncode == 1
    assert f"notes.md:1:see {LINK}" in result.stdout
    assert "A tracked file contains a session link." in result.stdout


def test_link_in_a_staged_but_uncommitted_file_fails(repo: Repo) -> None:
    (repo.path / "draft.md").write_text(f"{TRAILER}\n", encoding="utf-8")
    repo.git("add", "draft.md")
    assert repo.check().returncode == 1


def test_untracked_file_is_not_scanned(repo: Repo) -> None:
    (repo.path / "scratch.md").write_text(f"{LINK}\n", encoding="utf-8")
    assert repo.check().returncode == 0


def test_trailer_in_a_commit_message_since_base_fails(repo: Repo) -> None:
    repo.commit_file("a.txt", "a\n", f"feat: a\n\n{TRAILER}")
    result = repo.check()
    assert result.returncode == 1
    assert "A commit message on this branch (since origin/main) contains a session link." in result.stdout


def test_link_in_a_commit_already_on_base_is_not_reported(repo: Repo) -> None:
    repo.commit_file("a.txt", "a\n", f"feat: a\n\n{LINK}")
    repo.git("update-ref", "refs/remotes/origin/main", "HEAD")
    assert repo.check().returncode == 0


def test_message_file_argument_is_checked(repo: Repo, tmp_path: Path) -> None:
    message = tmp_path / "COMMIT_EDITMSG"
    message.write_text(f"fix: x\n\n{TRAILER}\n", encoding="utf-8")
    result = repo.check(str(message))
    assert result.returncode == 1
    assert "The commit message contains a session link." in result.stdout
    message.write_text("fix: x\n\nWhy: because\n", encoding="utf-8")
    assert repo.check(str(message)).returncode == 0


def test_missing_or_unreadable_message_file_fails(repo: Repo, tmp_path: Path) -> None:
    missing = repo.check(str(tmp_path / "nope"))
    assert missing.returncode == 1
    assert f"Cannot read the commit message file {tmp_path / 'nope'}." in missing.stdout
    directory = repo.check(str(tmp_path))  # a folder is not a readable message file
    assert directory.returncode == 1
    assert "Cannot read the commit message file" in directory.stdout


def test_require_base_is_accepted_in_any_position(repo: Repo, tmp_path: Path) -> None:
    message = tmp_path / "COMMIT_EDITMSG"
    message.write_text("fix: x\n\nWhy: because\n", encoding="utf-8")
    after = repo.check(str(message), "--require-base", base="origin/nowhere")
    assert after.returncode == 1
    assert "Base ref origin/nowhere does not resolve" in after.stdout
    assert "Cannot read" not in after.stdout  # the flag was not taken for a file name
    assert repo.check(str(message), "--require-base").returncode == 0
    message.write_text(f"fix: x\n\n{TRAILER}\n", encoding="utf-8")
    flagged = repo.check("--require-base", str(message))
    assert flagged.returncode == 1
    assert "The commit message contains a session link." in flagged.stdout


def test_missing_base_is_a_note_locally_and_a_failure_in_ci(repo: Repo) -> None:
    local = repo.check(base="origin/nowhere")
    assert local.returncode == 0
    assert "origin/nowhere does not resolve" in local.stderr
    ci = repo.check("--require-base", base="origin/nowhere")
    assert ci.returncode == 1
    assert "Base ref origin/nowhere does not resolve" in ci.stdout


def test_outside_a_git_repository_fails(tmp_path: Path) -> None:
    outside = tmp_path / "plain"
    outside.mkdir()
    env = git_env(tmp_path) | {"GIT_CEILING_DIRECTORIES": str(tmp_path)}
    result = subprocess.run(["bash", str(SCRIPT)], cwd=outside, env=env, capture_output=True, text=True)
    assert result.returncode == 1
    assert "git grep failed" in result.stdout


def test_this_workspace_has_no_tracked_link() -> None:
    if not (ROOT / ".git").exists():
        pytest.skip("not a git checkout")
    result = subprocess.run(
        ["bash", str(SCRIPT)], cwd=ROOT, env=dict(os.environ, BASE="HEAD"), capture_output=True, text=True
    )
    assert (result.returncode, result.stdout) == (0, "")

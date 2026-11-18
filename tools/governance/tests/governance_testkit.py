"""Helpers for the governance tests: hermetic git repos, the files under test, sample text.

Every repository lives under pytest's ``tmp_path`` (unique per test and per xdist
worker), never at a fixed /tmp path. Git runs with no global or system config, so a
developer's signing or hook settings cannot change the result, and with no network:
"origin" is a bare repository on disk.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
GUARD_SCRIPT = REPO_ROOT / ".circleci" / "check_no_session_links.sh"
GUARD_PATHS_HOOK = REPO_ROOT / ".claude" / "hooks" / "guard_paths.py"
NO_LINKS_HOOK = REPO_ROOT / ".claude" / "hooks" / "no_session_links.py"

# Forbidden strings are assembled from parts so this file never contains them whole.
DOT_AI = "claude" + ".ai"
SESSION_LINK = "https://" + DOT_AI + "/code/session_0123abcdEXAMPLE"
PROJECT_LINK = "https://" + DOT_AI + "/project/0000-example"
SESSION_TRAILER = "Claude-" + "Session: " + SESSION_LINK
BARE_TRAILER = "Claude-" + "Session: 0123abcdEXAMPLE"  # the trailer alone, no link in it
FOOTER = "\U0001f916 " + "Gene" + "rated with [Claude Code](https://claude" + ".com/claude-code)"
PLAIN_FOOTER = "Gene" + "rated by Claude Code"


def git_env(home: Path) -> dict[str, str]:
    """An environment for git with no user config, fixed identity and no CI variables."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_", "CIRCLE_"))}
    env.pop("GITHUB_TOKEN", None)
    gitconfig = home / "gitconfig"
    gitconfig.touch()
    env.update(
        GIT_CONFIG_GLOBAL=str(gitconfig),
        GIT_CONFIG_NOSYSTEM="1",
        GIT_AUTHOR_NAME="Test Author",
        GIT_AUTHOR_EMAIL="author@example.invalid",
        GIT_COMMITTER_NAME="Test Author",
        GIT_COMMITTER_EMAIL="author@example.invalid",
        HOME=str(home),
    )
    return env


@dataclass
class Repo:
    """A working repository with a bare ``origin`` beside it."""

    path: Path
    origin: Path
    env: dict[str, str]

    def git(self, *args: str) -> str:
        done = subprocess.run(
            ["git", *args], cwd=self.path, env=self.env, check=True, capture_output=True, text=True
        )
        return done.stdout

    def write(self, rel: str, text: str | bytes) -> None:
        target = self.path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(text, bytes):
            target.write_bytes(text)
        else:
            target.write_text(text, encoding="utf-8", newline="")

    def commit(self, message: str, rel: str = "notes.txt", text: str | None = None) -> str:
        """Commit a change to ``rel`` (default: append a line) with ``message`` verbatim."""
        if text is None:
            target = self.path / rel
            previous = target.read_text(encoding="utf-8") if target.exists() else ""
            text = previous + f"line {len(previous.splitlines()) + 1}\n"
        self.write(rel, text)
        self.git("add", "-A")
        msg_file = self.path.parent / "message.txt"
        msg_file.write_bytes(message.encode("utf-8"))
        self.git("commit", "--quiet", "--cleanup=verbatim", "-F", str(msg_file))
        return self.git("rev-parse", "--short", "HEAD").strip()

    def push_main(self) -> None:
        self.git("push", "--quiet", "origin", "main")

    def run_guard(self, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        env = dict(self.env)
        env.update(extra_env or {})
        return subprocess.run(
            ["bash", str(GUARD_SCRIPT)], cwd=self.path, env=env, capture_output=True, text=True
        )



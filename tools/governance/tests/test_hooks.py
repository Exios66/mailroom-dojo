"""Behaviour tests for the PreToolUse hooks in .claude/hooks/.

Each hook runs as Claude Code runs it: a subprocess fed the hook JSON on stdin. Exit 2
blocks the call; anything else lets it through. Both hooks must fail open on input they
cannot parse, so a hook bug never wedges a session.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from governance_testkit import (
    BARE_SESSION_ID,
    BARE_TRAILER,
    FOOTER,
    GUARD_PATHS_HOOK,
    NO_LINKS_HOOK,
    PLAIN_FOOTER,
    PROJECT_LINK,
    SESSION_LINK,
    SESSION_TRAILER,
)


def run_hook(hook: Path, payload: Any, *, root: Path | None = None, raw: str | None = None) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    if root is not None:
        env["CLAUDE_PROJECT_DIR"] = str(root)
    stdin = raw if raw is not None else json.dumps(payload)
    return subprocess.run(["python3", str(hook)], input=stdin, env=env, capture_output=True, text=True)


# --- no_session_links.py ---------------------------------------------------------


@pytest.mark.parametrize(
    ("tool_name", "tool_input"),
    [
        pytest.param("Bash", {"command": "git commit -m 'fix: x' -m '" + BARE_TRAILER + "'"}, id="bash-commit-trailer"),
        pytest.param("Bash", {"command": "gh pr create --title t --body '" + FOOTER + "'"}, id="bash-gh-footer"),
        pytest.param(
            "mcp__github__create_pull_request",
            {"owner": "o", "repo": "r", "title": "feat: x", "body": "Summary\n\n" + PLAIN_FOOTER.upper()},
            id="mcp-pr-body-uppercase",
        ),
        pytest.param(
            "mcp__github__update_pull_request",
            {"pullNumber": 3, "body": "ok\r\n" + SESSION_LINK + "\r\n"},
            id="mcp-update-crlf",
        ),
        pytest.param(
            "mcp__github__merge_pull_request",
            {"merge_method": "squash", "commit_title": "feat: x (#3)", "commit_message": "Why\n\n" + SESSION_TRAILER},
            id="mcp-squash-message",
        ),
        pytest.param(
            "mcp__github__push_files",
            {"files": [{"path": "a.md", "content": "see " + PROJECT_LINK}], "message": "docs: a"},
            id="mcp-nested-file-content",
        ),
        pytest.param(
            "mcp__plugin_engineering_github__add_issue_comment",
            {"body": "Fixed. " + SESSION_LINK},
            id="mcp-other-github-server",
        ),
        pytest.param("Write", {"file_path": "/x/docs/a.md", "content": FOOTER}, id="write-file"),
        pytest.param("Edit", {"file_path": "/x/a.md", "old_string": "a", "new_string": SESSION_LINK}, id="edit-new-string"),
        pytest.param(
            "MultiEdit",
            {"file_path": "/x/a.md", "edits": [{"old_string": "a", "new_string": "b"}, {"old_string": "c", "new_string": FOOTER}]},
            id="multiedit-second-edit",
        ),
        pytest.param(
            "NotebookEdit",
            {"notebook_path": "/x/n.ipynb", "cell_id": "c1", "new_source": "# " + BARE_TRAILER},
            id="notebookedit-source",
        ),
        pytest.param("bash", {"command": "echo " + SESSION_LINK}, id="lowercase-tool-name"),
        pytest.param("mcp__GitHub__create_pull_request", {"body": PLAIN_FOOTER}, id="mixed-case-mcp-server"),
        pytest.param("Bash", {"command": "git commit -m 'x' -m 'id " + BARE_SESSION_ID + "'"}, id="bare-session-id"),
        pytest.param("Bash", {"command": "grep -rn 'claude\\.ai/code' ."}, id="sweep-regex-spelling-a-link"),
    ],
)
def test_no_session_links_blocks(tool_name: str, tool_input: dict[str, Any]) -> None:
    result = run_hook(NO_LINKS_HOOK, {"tool_name": tool_name, "tool_input": tool_input})
    assert result.returncode == 2, result.stderr
    assert "Blocked" in result.stderr
    assert "session_0123" not in result.stderr and "/project/0000" not in result.stderr


@pytest.mark.parametrize(
    ("tool_name", "tool_input"),
    [
        pytest.param(
            "Bash",
            {"command": "git commit -m 'fix: x' -m 'Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>'"},
            id="bash-co-authored-by",
        ),
        pytest.param("Bash", {"command": "git grep -nE 'claude\\.ai|Claude-Session'"}, id="bash-sweep-regex"),
        pytest.param("Bash", {"command": "git grep -nE 'claude\\.a[i]/|Claude-[S]ession *:'"}, id="bash-sweep-regex-bracketed"),
        pytest.param(
            "MultiEdit",
            {"file_path": "/x/a.md", "edits": [{"old_string": SESSION_LINK, "new_string": "a link, in words"}]},
            id="multiedit-removes-link",
        ),
        pytest.param(
            "mcp__github__create_pull_request",
            {"title": "feat: x", "body": '<!-- claude-projects-attribution: {"github_login":"someone"} -->\n## Problem'},
            id="mcp-attribution-comment",
        ),
        pytest.param("Edit", {"file_path": "/x/a.md", "old_string": SESSION_LINK, "new_string": ""}, id="edit-removes-link"),
        pytest.param("Read", {"file_path": "/x/" + "a.md", "note": SESSION_LINK}, id="unchecked-tool"),
        pytest.param("mcp__other__thing", {"body": SESSION_LINK}, id="non-github-mcp"),
    ],
)
def test_no_session_links_allows(tool_name: str, tool_input: dict[str, Any]) -> None:
    result = run_hook(NO_LINKS_HOOK, {"tool_name": tool_name, "tool_input": tool_input})
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param("not json", id="not-json"),
        pytest.param("", id="empty"),
        pytest.param("[]", id="list"),
        pytest.param('{"tool_name": 5, "tool_input": {"command": "x"}}', id="bad-name"),
    ],
)
def test_no_session_links_fails_open(raw: str) -> None:
    assert run_hook(NO_LINKS_HOOK, None, raw=raw).returncode == 0


def load_hook_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_no_session_links_walk_survives_nesting_past_the_recursion_limit() -> None:
    """The walk itself must not raise RecursionError (the hook would then fail open).

    Built in memory, not through json: json on some Pythons refuses this depth on its own.
    """
    hook = load_hook_module(NO_LINKS_HOOK, "no_session_links_under_test")
    depth = sys.getrecursionlimit() * 3
    value: Any = SESSION_LINK
    for _ in range(depth):
        value = {"a": [value]}
    assert hook._strings(value) == [SESSION_LINK]
    assert hook.find_violation("mcp__github__push_files", value) is True


def test_no_session_links_blocks_nested_input_end_to_end() -> None:
    """Through the real hook process, at a depth every supported Python's json parses."""
    depth = 400
    raw = '{"tool_name": "mcp__github__push_files", "tool_input": ' + '{"a": [' * depth
    raw += json.dumps(SESSION_LINK) + "]}" * depth + "}"
    json.loads(raw)  # positive control: the input is valid JSON here
    result = run_hook(NO_LINKS_HOOK, None, raw=raw)
    assert result.returncode == 2, result.stderr[-300:]


def test_no_session_links_footer_pattern_cannot_be_made_to_backtrack() -> None:
    """A long run of separators after the footer's first words must not stall the hook."""
    hostile = "Gene" + "rated with " + "_" * 200_000 + " no name here"
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": hostile}})
    try:
        result = subprocess.run(
            ["python3", str(NO_LINKS_HOOK)], input=payload, capture_output=True, text=True, timeout=10
        )
    except subprocess.TimeoutExpired:
        pytest.fail("hook still running after 10s: the footer pattern backtracks")
    assert result.returncode == 0, result.stderr


# --- guard_paths.py --------------------------------------------------------------


@pytest.mark.parametrize(
    "rel",
    [
        ".env",
        "packages/engine/.env",
        ".env.local",
        "docs/../.env",
        "./config/../.env.production",
        "runs/r1/segments/w0.jsonl",
        "data/store/runs/r1/columns/events-0.parquet",
        "store/blobs/ab/cd/" + "ab" * 32,
        "store/archive/r1.tar.zst",
        "docs/reports/stage1-gate.md",
        "docs/stage1-gate-report.md",
        "docs/plans/record/stage1-gate-report.md",
        ".ENV",
        "config/.Env.Local",
        ".env/secret",
        "packages/engine/.env/nested/key",
        "prod.env",
        "config/Staging.ENV",
        "Docs/Reports/stage1-gate.md",
        "store/blobs/AB/CD/" + "AB" * 32,
        "store/archive/r1.TAR.ZST",
        "runs/r1/seals/seal-0.json",
        ".env.example/production",
        "RUNS/r1/SEGMENTS/w0.jsonl",
    ],
)
def test_guard_paths_blocks_inside_repo(tmp_path: Path, rel: str) -> None:
    result = run_hook(GUARD_PATHS_HOOK, {"tool_name": "Write", "tool_input": {"file_path": rel}}, root=tmp_path)
    assert result.returncode == 2, rel
    assert "Blocked edit" in result.stderr


@pytest.mark.parametrize(
    "path",
    [
        "/elsewhere/project-files/runs/autoresearch/v1/items.jsonl",
        "/elsewhere/store/runs/r9/segments/w1.2.jsonl",
        "/elsewhere/app/.env",
    ],
)
def test_guard_paths_blocks_stored_runs_and_env_outside_repo(tmp_path: Path, path: str) -> None:
    result = run_hook(GUARD_PATHS_HOOK, {"tool_name": "Edit", "tool_input": {"file_path": path}}, root=tmp_path)
    assert result.returncode == 2, path


@pytest.mark.parametrize(
    "rel",
    [
        ".env.example",
        "packages/engine/.env.example",
        ".ENV.EXAMPLE",
        ".envrc.md",
        "docs/environment.md",
        "src/dotenv.py",
        "docs/reports-index.md",
        "packages/record/src/mailroom_record/log.py",
        "docs/reports/README.md",
        "docs/runs.md",
        "config/policies/legacy.yaml",
        "CLAUDE.md",
    ],
)
def test_guard_paths_allows_source(tmp_path: Path, rel: str) -> None:
    result = run_hook(GUARD_PATHS_HOOK, {"tool_name": "Write", "tool_input": {"file_path": rel}}, root=tmp_path)
    assert result.returncode == 0, (rel, result.stderr)


@pytest.mark.parametrize(
    ("rel", "blocked"),
    [
        ("docs/reports/README.md", False),
        ("docs/reports/sub/README.md", True),
        ("docs/reports/README.md.bak", True),
        ("docs/reports/xREADME.md", True),
        ("docs/reports/README.mdx", True),
        ("docs/reports/README", True),
    ],
)
def test_guard_paths_readme_exception_is_exactly_the_reports_readme(tmp_path: Path, rel: str, blocked: bool) -> None:
    result = run_hook(GUARD_PATHS_HOOK, {"tool_name": "Write", "tool_input": {"file_path": rel}}, root=tmp_path)
    assert (result.returncode == 2) is blocked, rel


def _git(cwd: Path, *args: str) -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1",
               GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")
    subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True)


@pytest.fixture
def launch_and_worktree(tmp_path: Path) -> tuple[Path, Path]:
    """A checkout (where the session started) and a linked worktree of it."""
    launch = tmp_path / "launch"
    launch.mkdir()
    _git(launch, "init", "--quiet", "-b", "main")
    _git(launch, "config", "commit.gpgsign", "false")
    (launch / "README.md").write_text("x\n", encoding="utf-8")
    _git(launch, "add", "README.md")
    _git(launch, "commit", "--quiet", "-m", "init")
    worktree = tmp_path / "wt"
    _git(launch, "worktree", "add", "--quiet", "-b", "side", str(worktree))
    (worktree / "docs").mkdir()
    return launch, worktree


def test_guard_paths_applies_repo_rules_in_the_active_worktree(launch_and_worktree: tuple[Path, Path]) -> None:
    launch, worktree = launch_and_worktree
    target = str(worktree / "docs" / "reports" / "gate.md")
    payload = {"tool_name": "Write", "tool_input": {"file_path": target}}
    # Positive control: from the launch checkout alone, the worktree path is "outside".
    assert run_hook(GUARD_PATHS_HOOK, payload, root=launch).returncode == 0
    # With the payload's cwd in a subfolder of the worktree, the rule applies there.
    with_cwd = {**payload, "cwd": str(worktree / "docs")}
    result = run_hook(GUARD_PATHS_HOOK, with_cwd, root=launch)
    assert result.returncode == 2, result.stderr
    # And still in the launch checkout.
    in_launch = {"tool_name": "Write", "tool_input": {"file_path": str(launch / "docs" / "reports" / "x.md")},
                 "cwd": str(worktree)}
    assert run_hook(GUARD_PATHS_HOOK, in_launch, root=launch).returncode == 2


def test_guard_paths_outside_both_checkouts_is_unchanged(launch_and_worktree: tuple[Path, Path], tmp_path: Path) -> None:
    launch, worktree = launch_and_worktree
    elsewhere = tmp_path / "elsewhere" / "docs" / "reports" / "x.md"
    payload = {"tool_name": "Write", "tool_input": {"file_path": str(elsewhere)}, "cwd": str(worktree)}
    assert run_hook(GUARD_PATHS_HOOK, payload, root=launch).returncode == 0
    not_git = {**payload, "cwd": str(tmp_path)}  # a cwd outside any checkout adds no root
    assert run_hook(GUARD_PATHS_HOOK, not_git, root=launch).returncode == 0


def test_guard_paths_sees_through_a_symlinked_folder(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    (root / "docs").mkdir(parents=True)
    (root / "docs" / "reports").mkdir()
    (root / "shortcut").symlink_to(root / "docs" / "reports", target_is_directory=True)
    result = run_hook(
        GUARD_PATHS_HOOK, {"tool_name": "Write", "tool_input": {"file_path": "shortcut/gate.md"}}, root=root
    )
    assert result.returncode == 2


def test_guard_paths_docs_reports_rule_applies_only_inside_repo(tmp_path: Path) -> None:
    result = run_hook(
        GUARD_PATHS_HOOK,
        {"tool_name": "Write", "tool_input": {"file_path": "/elsewhere/docs/reports/x.md"}},
        root=tmp_path,
    )
    assert result.returncode == 0


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param("not json", id="not-json"),
        pytest.param('{"tool_input": "x"}', id="input-not-object"),
        pytest.param('{"tool_input": {"file_path": 7}}', id="path-not-string"),
    ],
)
def test_guard_paths_fails_open(tmp_path: Path, raw: str) -> None:
    assert run_hook(GUARD_PATHS_HOOK, None, raw=raw, root=tmp_path).returncode == 0

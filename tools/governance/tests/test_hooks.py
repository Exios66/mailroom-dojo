"""Behaviour tests for the PreToolUse hooks in .claude/hooks/.

Each hook runs as Claude Code runs it: a subprocess fed the hook JSON on stdin. Exit 2
blocks the call; anything else lets it through. Both hooks must fail open on input they
cannot parse, so a hook bug never wedges a session.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
from governance_testkit import (
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

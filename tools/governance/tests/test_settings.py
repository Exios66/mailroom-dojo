"""Checks on .claude/settings.json: hook wiring and the permission lists.

Claude Code compiles each PreToolUse ``matcher`` as a regular expression and matches it
against the whole tool name; these tests do the same, so a typo in a matcher (a tool that
silently loses its hook) or a dangerous command slipping into ``allow`` turns a test red.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from governance_testkit import REPO_ROOT, SETTINGS

GUARD = "guard_paths.py"
NO_LINKS = "no_session_links.py"


def load() -> dict:
    return json.loads(SETTINGS.read_text(encoding="utf-8"))


def hooks_for(tool_name: str) -> set[str]:
    """The hook files Claude Code would run before ``tool_name``."""
    found: set[str] = set()
    for entry in load()["hooks"]["PreToolUse"]:
        if re.fullmatch(entry["matcher"], tool_name):
            for hook in entry["hooks"]:
                found.update(re.findall(r"\.claude/hooks/([\w.]+\.py)", hook["command"]))
    return found


@pytest.mark.parametrize(
    ("tool_name", "expected"),
    [
        ("Bash", {NO_LINKS}),
        ("Edit", {GUARD, NO_LINKS}),
        ("Write", {GUARD, NO_LINKS}),
        ("MultiEdit", {GUARD, NO_LINKS}),
        ("NotebookEdit", {GUARD, NO_LINKS}),
        ("mcp__github__create_pull_request", {NO_LINKS}),
        ("mcp__github__update_pull_request", {NO_LINKS}),
        ("mcp__github__merge_pull_request", {NO_LINKS}),
        ("mcp__plugin_engineering_github__push_files", {NO_LINKS}),
        ("mcp__GitHub__create_pull_request", {NO_LINKS}),
        ("Read", set()),
        ("mcp__other__thing", set()),
        ("BashOutput", set()),
    ],
)
def test_matchers_route_each_tool_to_its_hooks(tool_name: str, expected: set[str]) -> None:
    assert hooks_for(tool_name) == expected


def test_every_hook_command_points_at_an_existing_file() -> None:
    for entry in load()["hooks"]["PreToolUse"]:
        for hook in entry["hooks"]:
            names = re.findall(r"\$CLAUDE_PROJECT_DIR/(\.claude/hooks/[\w.]+\.py)", hook["command"])
            assert names, hook["command"]
            for name in names:
                assert (REPO_ROOT / name).is_file(), name


def test_allow_list_holds_no_push_merge_tag_or_dangerous_git() -> None:
    allow = load()["permissions"]["allow"]
    forbidden = re.compile(
        r"git (push|merge|tag|rebase|reset|grep|fetch|config|commit)|gh (pr|release|api|issue)"
        r"|merge_pull_request|push_files|create_or_update_file|delete_file|comment",
        re.IGNORECASE,
    )
    hits = [rule for rule in allow if forbidden.search(rule)]
    assert not hits, hits


def test_mcp_comment_tools_are_denied_and_direct_writes_ask() -> None:
    perms = load()["permissions"]
    for tool in (
        "add_issue_comment",
        "add_reply_to_pull_request_comment",
        "add_comment_to_pending_review",
        "pull_request_review_write",
        "update_issue_comment",
    ):
        assert f"mcp__github__{tool}" in perms["deny"], tool
    for tool in ("merge_pull_request", "enable_pr_auto_merge", "push_files", "create_or_update_file", "delete_file"):
        assert f"mcp__github__{tool}" in perms["ask"], tool
    # The documented flow overwrites a new PR body with update_pull_request: never gate it.
    every_rule = perms["allow"] + perms["ask"] + perms["deny"]
    assert not [r for r in every_rule if "create_pull_request" in r or "update_pull_request" in r]


@pytest.mark.parametrize("flag", ["--no-index", "--output", "-O", "--open-files-in-pager", "--upload-pack"])
def test_dangerous_git_flags_are_denied(flag: str) -> None:
    assert f"Bash(git * {flag}*)" in load()["permissions"]["deny"]


def test_env_reads_are_denied_without_hiding_the_example() -> None:
    deny = [r for r in load()["permissions"]["deny"] if r.startswith("Read(")]
    for name in (".env", ".env.local", ".env.production", ".env.development", ".env.test"):
        assert f"Read(./{name})" in deny and f"Read(./**/{name})" in deny, name
    # No negation (not supported) and no wildcard that would also deny .env.example.
    assert not [r for r in deny if "!" in r or ".env.*" in r or ".env*" in r]


def test_settings_is_the_file_claude_code_loads() -> None:
    assert SETTINGS == Path(REPO_ROOT, ".claude", "settings.json")
    assert isinstance(load(), dict)

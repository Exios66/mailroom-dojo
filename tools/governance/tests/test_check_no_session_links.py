"""Behaviour tests for .circleci/check_no_session_links.sh against real git repositories.

Each test builds a repository under tmp_path with a bare local "origin", runs the real
script with bash, and asserts the exit code, what it names, and that it never echoes
the forbidden text into the (public) CI log. The GitHub API is replaced by a stub
``curl`` on PATH, so no test touches the network.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
from governance_testkit import (
    BARE_TRAILER,
    DOT_AI,
    FOOTER,
    GUARD_SCRIPT,
    NO_LINKS_HOOK,
    PLAIN_FOOTER,
    PROJECT_LINK,
    REPO_ROOT,
    SESSION_LINK,
    SESSION_TRAILER,
    Repo,
)

CLEAN_MESSAGE = (
    "feat(record): add a thing\n\nWhy: because.\nWhat changed: a thing.\n"
    "Testing: pytest -> 1 passed\nRefs: none\n\n"
    "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>\n"
)


def assert_no_leak(result: subprocess.CompletedProcess[str]) -> None:
    """The script names commits and files, never the matched text."""
    out = result.stdout + result.stderr
    assert "session_0123" not in out
    assert "/project/0000" not in out
    assert DOT_AI + "/" not in out.lower()


def on_branch(repo: Repo, name: str = "feature") -> None:
    repo.git("switch", "--quiet", "-c", name)


# --- commit messages -------------------------------------------------------------


def test_clean_branch_passes(repo: Repo) -> None:
    on_branch(repo)
    repo.commit(CLEAN_MESSAGE)
    repo.commit("docs: mention a claude" + ".ai session link in words only\n")
    result = repo.run_guard()
    assert result.returncode == 0, result.stderr
    assert "ok: no session links or footers in commit messages (origin/main..HEAD)" in result.stdout
    assert "ok: no session links or footers in tracked files" in result.stdout


def test_trailer_in_middle_commit_body_fails_and_names_only_that_commit(repo: Repo) -> None:
    on_branch(repo)
    repo.commit(CLEAN_MESSAGE)
    middle = repo.commit("fix: middle\n\nWhy: x.\n\n" + BARE_TRAILER + "\n")
    repo.commit(CLEAN_MESSAGE)
    result = repo.run_guard()
    assert result.returncode == 1
    assert "error: 1 commit message(s) in origin/main..HEAD" in result.stderr
    named = re.findall(r"^  ([0-9a-f]{7,})$", result.stderr, flags=re.M)
    assert named == [middle]
    assert_no_leak(result)


@pytest.mark.parametrize(
    "bad_text",
    [
        pytest.param(SESSION_TRAILER, id="session-trailer"),
        pytest.param(BARE_TRAILER, id="trailer-without-link"),
        pytest.param(BARE_TRAILER.upper(), id="trailer-uppercase"),
        pytest.param("claude-" + "session : abc", id="trailer-lowercase-spaced"),
        pytest.param("See " + SESSION_LINK.upper(), id="link-uppercase"),
        pytest.param("See " + PROJECT_LINK, id="project-link"),
        pytest.param("See https://www." + DOT_AI, id="bare-site"),
        pytest.param("see " + DOT_AI + "/share/abc", id="link-without-scheme"),
        pytest.param(FOOTER, id="footer-markdown"),
        pytest.param(PLAIN_FOOTER, id="footer-plain"),
        pytest.param("> **" + PLAIN_FOOTER.upper() + "**", id="footer-quoted-uppercase"),
    ],
)
def test_each_forbidden_form_fails(repo: Repo, bad_text: str) -> None:
    on_branch(repo)
    repo.commit("fix: x\n\nWhy: y.\n\n" + bad_text + "\n")
    result = repo.run_guard()
    assert result.returncode == 1, result.stdout
    assert_no_leak(result)


def test_windows_line_endings_are_still_caught(repo: Repo) -> None:
    on_branch(repo)
    repo.commit("fix: x\r\n\r\nWhy: y.\r\n\r\n" + BARE_TRAILER + "\r\n")
    result = repo.run_guard()
    assert result.returncode == 1
    assert_no_leak(result)


@pytest.mark.parametrize(
    "ok_text",
    [
        pytest.param("Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>", id="co-authored-by"),
        pytest.param('<!-- claude-projects-attribution: {"github_login":"someone"} -->', id="attribution-comment"),
        pytest.param("Never add a session trailer or a claude" + ".ai link.", id="rule-in-words"),
        pytest.param("Read https://docs.example.com/claude-code", id="other-site"),
    ],
)
def test_allowed_text_passes(repo: Repo, ok_text: str) -> None:
    on_branch(repo)
    repo.commit("docs: x\n\n" + ok_text + "\n")
    repo.commit("docs: y\n", rel="docs/rules.md", text=ok_text + "\n")
    result = repo.run_guard()
    assert result.returncode == 0, result.stderr


def test_old_link_on_main_does_not_fail_a_branch(repo: Repo) -> None:
    repo.commit("chore: old\n\n" + SESSION_TRAILER + "\n")
    repo.commit(CLEAN_MESSAGE)
    repo.push_main()
    on_branch(repo)
    repo.commit(CLEAN_MESSAGE)
    result = repo.run_guard()
    assert result.returncode == 0, result.stderr


def test_main_checks_only_head(repo: Repo) -> None:
    repo.commit("chore: old\n\n" + SESSION_TRAILER + "\n")
    repo.commit(CLEAN_MESSAGE)
    clean = repo.run_guard({"CIRCLE_BRANCH": "main"})
    assert clean.returncode == 0, clean.stderr
    assert "commit messages (HEAD)" in clean.stdout
    bad = repo.commit("feat: squash (#4)\n\nWhat changed: x.\n\n" + FOOTER + "\n")
    result = repo.run_guard({"CIRCLE_BRANCH": "main"})
    assert result.returncode == 1
    assert f"  {bad}" in result.stderr
    assert_no_leak(result)


def test_branch_fails_closed_when_origin_main_cannot_be_fetched(repo: Repo) -> None:
    on_branch(repo)
    repo.commit(CLEAN_MESSAGE)
    shutil.rmtree(repo.origin)
    result = repo.run_guard()
    assert result.returncode == 1
    assert "cannot fetch origin/main" in result.stderr


# --- tracked files ---------------------------------------------------------------


def test_link_in_tracked_file_fails_and_names_the_file(repo: Repo) -> None:
    on_branch(repo)
    repo.commit(CLEAN_MESSAGE, rel="docs/notes.md", text="# Notes\r\nSee " + SESSION_LINK + "\r\n")
    result = repo.run_guard()
    assert result.returncode == 1
    assert "  docs/notes.md" in result.stderr
    assert_no_leak(result)


def test_link_in_file_already_on_main_still_fails(repo: Repo) -> None:
    # A tracked file is scanned whatever commit added it: main must stay clean too.
    repo.commit(CLEAN_MESSAGE, rel="old.md", text=FOOTER + "\n")
    repo.push_main()
    result = repo.run_guard({"CIRCLE_BRANCH": "main"})
    assert result.returncode == 1
    assert "  old.md" in result.stderr


def test_untracked_file_is_ignored(repo: Repo) -> None:
    on_branch(repo)
    repo.commit(CLEAN_MESSAGE)
    repo.write("scratch.txt", SESSION_LINK + "\n")
    result = repo.run_guard()
    assert result.returncode == 0, result.stderr


# --- pull request title and description -------------------------------------------


def stub_curl(tmp_path: Path, responses: dict[str, dict[str, str] | None]) -> dict[str, str]:
    """Put a fake ``curl`` first on PATH that serves ``responses`` keyed by PR number.

    A value of None makes the stub fail like ``curl -f`` on an HTTP error.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    data = tmp_path / "responses.json"
    data.write_text(json.dumps(responses), encoding="utf-8")
    fake = bin_dir / "curl"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        f"responses = json.load(open({str(data)!r}, encoding='utf-8'))\n"
        "url = sys.argv[-1]\n"
        "number = url.rstrip('/').rsplit('/', 1)[-1]\n"
        "assert '/pulls/' in url, url\n"
        "body = responses.get(number)\n"
        "if body is None:\n"
        "    sys.exit(22)\n"
        "print(json.dumps(body))\n",
        encoding="utf-8",
    )
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    return {"PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"}


PR = "https://github.com/example-owner/mailroom-dojo/pull/{}"


def test_clean_pr_description_passes(repo: Repo, tmp_path: Path) -> None:
    on_branch(repo)
    repo.commit(CLEAN_MESSAGE)
    env = stub_curl(tmp_path, {"7": {"title": "feat: x", "body": "## Problem\nfine\r\n"}})
    result = repo.run_guard({**env, "CIRCLE_PULL_REQUEST": PR.format(7)})
    assert result.returncode == 0, result.stderr
    assert f"ok: no session links or footers in the title or description of {PR.format(7)}" in result.stdout


@pytest.mark.parametrize(
    "pr",
    [
        pytest.param({"title": "feat: x", "body": "Summary\n\n" + FOOTER + "\n\n" + SESSION_LINK}, id="footer-in-body"),
        pytest.param({"title": "feat: see " + PROJECT_LINK, "body": ""}, id="link-in-title"),
        pytest.param({"title": "feat: x", "body": "ok\r\n" + SESSION_TRAILER.lower() + "\r\n"}, id="trailer-crlf"),
    ],
)
def test_dirty_pr_fails_without_echoing_it(repo: Repo, tmp_path: Path, pr: dict[str, str]) -> None:
    on_branch(repo)
    repo.commit(CLEAN_MESSAGE)
    env = stub_curl(tmp_path, {"8": pr})
    result = repo.run_guard({**env, "CIRCLE_PULL_REQUEST": PR.format(8)})
    assert result.returncode == 1
    assert f"error: the title or description of {PR.format(8)}" in result.stderr
    assert_no_leak(result)


def test_every_listed_pr_is_checked(repo: Repo, tmp_path: Path) -> None:
    on_branch(repo)
    repo.commit(CLEAN_MESSAGE)
    env = stub_curl(
        tmp_path,
        {"1": {"title": "a", "body": "clean"}, "2": {"title": "b", "body": PLAIN_FOOTER}},
    )
    result = repo.run_guard(
        {**env, "CIRCLE_PULL_REQUESTS": f"{PR.format(1)}, {PR.format(2)}", "CIRCLE_PULL_REQUEST": PR.format(1)}
    )
    assert result.returncode == 1
    assert PR.format(2) in result.stderr
    assert f"description of {PR.format(1)}" in result.stdout


def test_unreadable_pr_warns_but_does_not_fail(repo: Repo, tmp_path: Path) -> None:
    on_branch(repo)
    repo.commit(CLEAN_MESSAGE)
    env = stub_curl(tmp_path, {})
    result = repo.run_guard({**env, "CIRCLE_PULL_REQUEST": PR.format(9)})
    assert result.returncode == 0, result.stderr
    assert "warning: could not read" in result.stderr


# --- the pattern itself and the repository's own files ----------------------------


def script_pattern() -> str:
    match = re.search(r"^PATTERN='([^']+)'$", GUARD_SCRIPT.read_text(encoding="utf-8"), flags=re.M)
    assert match, "PATTERN line not found in the guard script"
    return match.group(1)


CORPUS_BAD = [
    SESSION_LINK,
    PROJECT_LINK,
    SESSION_TRAILER,
    BARE_TRAILER,
    FOOTER,
    PLAIN_FOOTER,
    "x " + DOT_AI.upper() + "/CHAT/1",
    "https://claude" + ".com/project/abc",
]
CORPUS_OK = [
    "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>",
    "a claude" + ".ai session link, described in words",
    "pattern='claude\\.a[i]/[c]ode|Claude-[S]ession:'",
    "Generated report for Claude Code users",  # no "with"/"by" before the name
    "https://claude" + ".com/pricing",
]


@pytest.mark.parametrize(
    "text",
    CORPUS_BAD + CORPUS_OK,
    # Numbered ids: a test id holding the forbidden text would print it into the CI log.
    ids=[f"bad-{i}" for i in range(len(CORPUS_BAD))] + [f"ok-{i}" for i in range(len(CORPUS_OK))],
)
def test_script_and_hook_patterns_agree(text: str) -> None:
    grep = subprocess.run(["grep", "-iE", script_pattern()], input=text + "\n", capture_output=True, text=True)
    assert grep.returncode in (0, 1), grep.stderr
    hook = subprocess.run(
        ["python3", str(NO_LINKS_HOOK)],
        input=json.dumps({"tool_name": "Bash", "tool_input": {"command": text}}),
        capture_output=True,
        text=True,
    )
    expected_bad = text in CORPUS_BAD
    assert (grep.returncode == 0) is expected_bad, f"script pattern on {text!r}"
    assert (hook.returncode == 2) is expected_bad, f"hook on {text!r}"


def test_repository_passes_its_own_scan(repo: Repo) -> None:
    """Copy every file this checkout tracks (or would add) into a repo and scan it on main."""
    listed = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=REPO_ROOT,
        capture_output=True,
        check=False,
    )
    if listed.returncode != 0:
        pytest.skip("not a git checkout")
    files = [f for f in listed.stdout.decode().split("\0") if f and (REPO_ROOT / f).is_file()]
    assert ".circleci/check_no_session_links.sh" in files
    for rel in files:
        target = repo.path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(REPO_ROOT / rel, target)
    repo.commit(CLEAN_MESSAGE)
    result = repo.run_guard({"CIRCLE_BRANCH": "main"})
    assert result.returncode == 0, result.stderr

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
    BARE_SESSION_ID,
    BARE_TRAILER,
    DOT_AI,
    FOOTER,
    GUARD_SCRIPT,
    LEAK_FRAGMENTS,
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
    """The script names commits and files, never the matched text (links, trailers, footers)."""
    out = (result.stdout + result.stderr).lower()
    leaked = [i for i, fragment in enumerate(LEAK_FRAGMENTS) if fragment in out]
    # Report indexes, not the fragments: the assertion message lands in the CI log.
    assert not leaked, f"guard output echoes forbidden fragment(s) #{leaked}"


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
        pytest.param("id " + BARE_SESSION_ID, id="bare-session-id"),
        pytest.param("Claude" + " Session: abc", id="trailer-space"),
        pytest.param("Claude" + "_Session: abc", id="trailer-underscore"),
        pytest.param("Gene" + "rated using [Claude Code](x)", id="footer-using"),
    ],
)
def test_each_forbidden_form_fails(repo: Repo, bad_text: str) -> None:
    on_branch(repo)
    repo.commit("fix: x\n\nWhy: y.\n\n" + bad_text + "\n")
    result = repo.run_guard()
    assert result.returncode == 1, result.stdout
    assert_no_leak(result)


def test_script_matches_in_the_c_locale_like_the_hook(repo: Repo) -> None:
    """The script exports LC_ALL=C, so a caller's UTF-8 locale cannot widen [[:alnum:]].

    Under C.UTF-8, grep counts non-ASCII letters as alphanumeric and would call this a
    session id; the hook (re.ASCII) does not. Both must agree that it is clean.
    """
    probe = subprocess.run(
        ["grep", "-ciE", "x[[:alnum:]]"], input="x\u00e9\n", capture_output=True, text=True,
        env={**os.environ, "LC_ALL": "C.UTF-8"},
    )
    if probe.stdout.strip() != "1":
        pytest.skip("no C.UTF-8 locale with Unicode character classes here")
    on_branch(repo)
    repo.commit("docs: x\n\nsession" + "_" + "\u00e9" * 24 + "\n")
    result = repo.run_guard({"LC_ALL": "C.UTF-8", "LANG": "C.UTF-8"})
    assert result.returncode == 0, result.stderr


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


def test_main_checks_every_commit_a_merge_brought_in(repo: Repo) -> None:
    repo.git("switch", "--quiet", "-c", "side")
    bad = repo.commit("fix: side\n\n" + BARE_TRAILER + "\n", rel="side.txt")
    repo.commit(CLEAN_MESSAGE, rel="side.txt")
    repo.git("switch", "--quiet", "main")
    repo.commit(CLEAN_MESSAGE)
    repo.git("merge", "--quiet", "--no-ff", "-m", "Merge branch side", "side")
    result = repo.run_guard({"CIRCLE_BRANCH": "main"})
    assert result.returncode == 1, result.stdout
    assert "HEAD --not HEAD^1" in result.stderr
    named = re.findall(r"^  ([0-9a-f]{7,})$", result.stderr, flags=re.M)
    assert named == [bad]
    assert_no_leak(result)


def test_main_checks_a_merge_commits_own_message(repo: Repo) -> None:
    repo.git("switch", "--quiet", "-c", "side")
    repo.commit(CLEAN_MESSAGE, rel="side.txt")
    repo.git("switch", "--quiet", "main")
    repo.commit(CLEAN_MESSAGE)
    msg = repo.path.parent / "merge-message.txt"
    msg.write_text("Merge branch side\n\n" + BARE_TRAILER + "\n", encoding="utf-8")
    repo.git("merge", "--quiet", "--no-ff", "--cleanup=verbatim", "-F", str(msg), "side")
    merge = repo.git("rev-parse", "--short", "HEAD").strip()
    result = repo.run_guard({"CIRCLE_BRANCH": "main"})
    assert result.returncode == 1, result.stdout
    named = re.findall(r"^  ([0-9a-f]{7,})$", result.stderr, flags=re.M)
    assert named == [merge]
    assert_no_leak(result)


def shallow_clone(repo: Repo, tmp_path: Path) -> Repo:
    """A depth-1 clone of origin's main, with signing off like the fixture repo."""
    clone = tmp_path / "shallow"
    subprocess.run(
        ["git", "clone", "--quiet", "--depth", "1", "--branch", "main", f"file://{repo.origin}", str(clone)],
        env=repo.env,
        check=True,
    )
    r = Repo(path=clone, origin=repo.origin, env=repo.env)
    r.git("config", "commit.gpgsign", "false")
    assert r.git("rev-parse", "--is-shallow-repository").strip() == "true"
    return r


def test_shallow_clone_is_unshallowed_before_checking(repo: Repo, tmp_path: Path) -> None:
    # main's HEAD is a merge whose bad commit a depth-1 clone cannot see.
    repo.git("switch", "--quiet", "-c", "side")
    bad = repo.commit("fix: side\n\n" + FOOTER + "\n", rel="side.txt")
    repo.git("switch", "--quiet", "main")
    repo.commit(CLEAN_MESSAGE)
    repo.git("merge", "--quiet", "--no-ff", "-m", "Merge branch side", "side")
    repo.push_main()
    clone = shallow_clone(repo, tmp_path)
    result = clone.run_guard({"CIRCLE_BRANCH": "main"})
    assert result.returncode == 1, result.stdout
    assert f"  {bad}" in result.stderr
    assert clone.git("rev-parse", "--is-shallow-repository").strip() == "false"


def test_shallow_clone_fails_when_it_cannot_be_unshallowed(repo: Repo, tmp_path: Path) -> None:
    repo.commit(CLEAN_MESSAGE)
    repo.push_main()
    clone = shallow_clone(repo, tmp_path)
    shutil.rmtree(repo.origin)
    for branch in ("main", "feature"):
        result = clone.run_guard({"CIRCLE_BRANCH": branch})
        assert result.returncode == 1, (branch, result.stdout)
        assert "shallow" in result.stderr


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


def test_git_grep_error_fails(repo: Repo, tmp_path: Path) -> None:
    """A git grep that errors (exit > 1) must fail the run, not read as "no match"."""
    real_git = shutil.which("git")
    assert real_git
    bin_dir = tmp_path / "gitbin"
    bin_dir.mkdir()
    fake = bin_dir / "git"
    fake.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = grep ]; then echo "fatal: simulated" >&2; exit 2; fi\n'
        f'exec {real_git} "$@"\n',
        encoding="utf-8",
    )
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    on_branch(repo)
    repo.commit(CLEAN_MESSAGE)
    result = repo.run_guard({"PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"})
    assert result.returncode == 1, result.stdout
    assert "git grep failed with exit 2" in result.stderr
    assert "ok: no session links or footers in tracked files" not in result.stdout


# --- pull request title and description -------------------------------------------


def stub_curl(tmp_path: Path, responses: dict[str, object]) -> dict[str, str]:
    """Put a fake ``curl`` first on PATH that serves ``responses`` keyed by PR number.

    A value of None makes the stub fail like ``curl -f`` on an HTTP error; a plain string
    is printed as is (to test a body that is not JSON). Every call appends its argv, and
    the config it read on stdin for ``--config -``, to ``curl-calls.jsonl``.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    data = tmp_path / "responses.json"
    data.write_text(json.dumps(responses), encoding="utf-8")
    calls = tmp_path / "curl-calls.jsonl"
    fake = bin_dir / "curl"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        f"responses = json.load(open({str(data)!r}, encoding='utf-8'))\n"
        "args = sys.argv[1:]\n"
        "config = sys.stdin.read() if '--config' in args else ''\n"
        f"with open({str(calls)!r}, 'a', encoding='utf-8') as log:\n"
        "    log.write(json.dumps({'argv': args, 'config': config}) + '\\n')\n"
        "url = args[-1]\n"
        "number = url.rstrip('/').rsplit('/', 1)[-1]\n"
        "assert '/pulls/' in url, url\n"
        "body = responses.get(number)\n"
        "if body is None:\n"
        "    sys.exit(22)\n"
        "print(body if isinstance(body, str) else json.dumps(body))\n",
        encoding="utf-8",
    )
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    return {"PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}"}


def curl_calls(tmp_path: Path) -> list[dict[str, object]]:
    path = tmp_path / "curl-calls.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


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


@pytest.mark.parametrize(
    "response",
    [pytest.param(None, id="http-error"), pytest.param("<html>not json</html>", id="not-json")],
)
def test_unreadable_pr_without_token_warns_but_does_not_fail(
    repo: Repo, tmp_path: Path, response: str | None
) -> None:
    on_branch(repo)
    repo.commit(CLEAN_MESSAGE)
    env = stub_curl(tmp_path, {"9": response})
    result = repo.run_guard({**env, "CIRCLE_PULL_REQUEST": PR.format(9)})
    assert result.returncode == 0, result.stderr
    assert "warning: could not read" in result.stderr
    assert "Traceback" not in result.stderr


TOKEN = "ghp_" + "TESTTOKENVALUE0123456789"


@pytest.mark.parametrize(
    "response",
    [pytest.param(None, id="http-error"), pytest.param("<html>not json</html>", id="not-json")],
)
def test_unreadable_pr_with_token_fails(repo: Repo, tmp_path: Path, response: str | None) -> None:
    on_branch(repo)
    repo.commit(CLEAN_MESSAGE)
    env = stub_curl(tmp_path, {"9": response})
    result = repo.run_guard({**env, "CIRCLE_PULL_REQUEST": PR.format(9), "GITHUB_TOKEN": TOKEN})
    assert result.returncode == 1, result.stdout
    assert "error: could not read" in result.stderr
    assert "Traceback" not in result.stderr
    assert TOKEN not in result.stdout + result.stderr


def test_token_reaches_curl_on_stdin_and_is_never_printed(repo: Repo, tmp_path: Path) -> None:
    on_branch(repo)
    repo.commit(CLEAN_MESSAGE)
    env = stub_curl(tmp_path, {"7": {"title": "feat: x", "body": "clean"}})
    result = repo.run_guard({**env, "CIRCLE_PULL_REQUEST": PR.format(7), "GITHUB_TOKEN": TOKEN})
    assert result.returncode == 0, result.stderr
    assert TOKEN not in result.stdout + result.stderr
    calls = curl_calls(tmp_path)
    assert len(calls) == 1
    assert all(TOKEN not in arg for arg in calls[0]["argv"])  # never on the command line
    assert f"Authorization: Bearer {TOKEN}" in calls[0]["config"]  # positive control


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
    BARE_SESSION_ID,
    "x " + DOT_AI.upper() + "/CHAT/1",
    "https://claude" + ".com/project/abc",
    "https://claude" + ".com/session/abc",
    "claude" + ".com/code/abc",
    "claude" + ".com/chat/abc",
    "claude" + ".com/artifact/abc",
    "claude" + ".com/share/abc",
    "Claude" + " Session: abc",
    "Claude" + "_Session: abc",
    "https://claude" + "&#46;ai/code/x",
    "https://claude" + "&#x2E;ai",
    "see claude" + "\\.ai/code/x",
    "https://claude" + "%2Eai/code/x",
    "claude" + ".ai%2Fcode%2Fx",
    "**" + "Gene" + "rated with Claude Code**",
    "Gene" + "rated using Claude Code",
    "Gene" + "rated-with-Claude-Code",
    "Gene" + "rated with *Claude Code*",
    "Gene" + "rated with **Claude Code**",
    "Gene" + "rated with _Claude_ Code",
    "Gene" + "rated with [**Claude Code**](x)",
]
CORPUS_OK = [
    "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>",
    "a claude" + ".ai session link, described in words",
    "pattern='claude\\.a[i]/[c]ode|Claude-[S]ession:'",
    "Generated report for Claude Code users",  # no "with"/"by"/"using" before the name
    "https://claude" + ".com/pricing",
    "session" + "_id_for_the_run_in_this_test",  # underscores break the run
    "session" + "_0123456789abcdefABC",  # 19 characters: too short for an id
    "a Claude" + " session without a colon",
]
# Known limits, documented in AGENTS.md: both guards are line based and ASCII, so these
# pass both. The test pins that they agree, so a change to one side shows up here.
CORPUS_KNOWN_LIMITS = [
    "Gene" + "rated with\nClaude Code",  # footer split across two lines
    "Gene" + "rated with\u00a0Claude Code",  # non-breaking space
]

CORPUS = (
    [(text, True) for text in CORPUS_BAD]
    + [(text, False) for text in CORPUS_OK]
    + [(text, False) for text in CORPUS_KNOWN_LIMITS]
)
CORPUS_IDS = (
    [f"bad-{i}" for i in range(len(CORPUS_BAD))]
    + [f"ok-{i}" for i in range(len(CORPUS_OK))]
    + [f"known-limit-{i}" for i in range(len(CORPUS_KNOWN_LIMITS))]
)


# Numbered ids, and messages without the text: either would print it into the CI log.
@pytest.mark.parametrize(("text", "expected_bad"), CORPUS, ids=CORPUS_IDS)
def test_script_and_hook_patterns_agree(text: str, expected_bad: bool) -> None:
    grep = subprocess.run(
        ["grep", "-iE", script_pattern()],
        input=text + "\n",
        capture_output=True,
        text=True,
        env={**os.environ, "LC_ALL": "C"},  # as the script sets it
    )
    assert grep.returncode in (0, 1), "grep rejected the script pattern"
    hook = subprocess.run(
        ["python3", str(NO_LINKS_HOOK)],
        input=json.dumps({"tool_name": "Bash", "tool_input": {"command": text}}),
        capture_output=True,
        text=True,
    )
    assert (grep.returncode == 0) is expected_bad, "script pattern disagrees with the expected verdict"
    assert (hook.returncode == 2) is expected_bad, "hook disagrees with the expected verdict"


def test_repository_passes_its_own_scan(repo: Repo) -> None:
    """Copy every file this checkout tracks into a repo and scan it on main.

    Tracked files only, so untracked cruft (caches, scratch files) cannot change the result.
    """
    listed = subprocess.run(
        ["git", "ls-files", "-z", "--cached"],
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

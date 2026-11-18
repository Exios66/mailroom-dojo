#!/usr/bin/env bash
# Fails when a commit message, an open pull request's title or description, or a tracked
# file carries a link to a Claude session, project, thread, artifact or share page, a
# bare session id, a session trailer, or a Claude Code footer. Run it from the repository
# root, locally before every push. Once the wiring follow-up in AGENTS.md section 12
# lands, the CircleCI job `no-session-links` runs it on every build.
#
#   On main:           the commit at HEAD (the squash-merge message) and, when HEAD is a
#                      merge commit, every commit that merge brought in; then every
#                      tracked file. Older commits pushed straight to main are not
#                      re-checked.
#   On other branches: every commit not yet on origin/main, every pull request CircleCI
#                      reports for the build, and every tracked file.
#   For each checked commit, both its message and the lines it adds to files, so text
#   added by one commit and removed by a later one still fails (history keeps it).
#   A grep or git grep that errors is a failed scan, never a clean one.
#
# A shallow clone is unshallowed first; if that fails the script fails, because a
# shallow history would hide commits and give a false "ok".
#
# Pull requests are read from the GitHub API. The owner should add a read-only
# GITHUB_TOKEN to the CircleCI project: with a token set, a PR that cannot be read fails
# the build; without one, it only warns (unauthenticated rate limits are routine). The
# token is handed to curl on stdin (--config -), never on its command line. CircleCI does
# not rebuild when a PR description is edited, so re-run the job after opening the PR and
# after every body edit.
#
# It names offending commits, pull requests and files, and never prints the matched
# text: CI logs are public. The bracketed letters in PATTERN keep this file (and docs
# that quote the pattern) from matching itself. The PreToolUse hook
# .claude/hooks/no_session_links.py carries the same pattern; keep them in step
# (tools/governance/tests checks that they agree).
#
# Limits (it guards against accidents, not adversaries): matching is line based and
# ASCII, so a footer split across two lines, a non-breaking space, Unicode lookalike
# letters or zero-width characters are not caught; nor are annotated tag messages, git
# notes, author names or non-UTF-8 text files. See AGENTS.md, "The session-link guard".
set -euo pipefail
export LC_ALL=C

# Dot between "claude" and the top-level domain: plain, markdown-escaped, an HTML entity
# or percent-encoded. Separators inside a footer: spaces, hyphens and markdown emphasis,
# bounded ({1,8}) so the Python hook cannot backtrack for long on a hostile run. A link
# needs a host boundary after the domain, so a longer host name (claude.ai<more>) passes.
PATTERN='https?://([[:alnum:]-]+(\.|\\\.|&#0*46;|&#x0*2e;|%2e))*claude(\.|\\\.|&#0*46;|&#x0*2e;|%2e)a[i]([^[:alnum:]._-]|[.]([^[:alnum:]_-]|$)|$)|claude(\.|\\\.|&#0*46;|&#x0*2e;|%2e)a[i](/|%2f)[[:alnum:]]|claude(\.|\\\.|&#0*46;|&#x0*2e;|%2e)co[m](/|%2f)(claude-code|code|session|project|chat|artifact|share)|Claude[-_ ][S]ession[[:space:]]*:|[s]ession_[[:alnum:]]{20,}|G[e]nerated[-_*[:space:]]{1,8}(with|by|using)[-_*[:space:]]{1,8}\[?[*_]{0,4}Claude[-_*[:space:]]{1,8}Code'
status=0

# scan TEXT: 0 = match, 1 = clean, 2 = grep itself failed (never treated as clean).
scan() {
  local rc=0
  # grep without -q reads all input, so the writer never dies of SIGPIPE under pipefail.
  printf '%s\n' "$1" | grep -iE "$PATTERN" >/dev/null || rc=$?
  if [ "$rc" -le 1 ]; then return "$rc"; fi
  return 2
}

# Lines a commit adds (no headers, no context). A merge is diffed against its first
# parent; binary files show only "Binary files differ".
added_lines() {
  if git rev-parse --verify --quiet "$1^2" >/dev/null; then
    git diff --no-color --no-ext-diff --unified=0 "$1^1" "$1"
  else
    git diff-tree -p --root --no-color --no-ext-diff --unified=0 --no-commit-id "$1"
  fi | awk '/^diff /{h=1} /^@@/{h=0; next} !h && /^\+/{print substr($0, 2)}'
}

# 0. A shallow clone hides history: unshallow it or stop.
if [ "$(git rev-parse --is-shallow-repository)" = "true" ]; then
  if ! git fetch --quiet --unshallow >/dev/null 2>&1; then
    echo "error: this clone is shallow and 'git fetch --unshallow' failed; commits not checked" >&2
    exit 1
  fi
  if [ "$(git rev-parse --is-shallow-repository)" = "true" ]; then
    echo "error: this clone is still shallow after 'git fetch --unshallow'; commits not checked" >&2
    exit 1
  fi
fi

# 1. Commit messages.
branch="${CIRCLE_BRANCH:-$(git rev-parse --abbrev-ref HEAD)}"
if [ "$branch" = "main" ]; then
  if ! git rev-parse --verify --quiet HEAD >/dev/null; then
    echo "error: cannot read HEAD; commits not checked" >&2
    exit 1
  fi
  if git rev-parse --verify --quiet 'HEAD^2' >/dev/null; then
    # A merge commit: check it and every commit it brought in.
    range="HEAD --not HEAD^1"
    if ! commits=$(git rev-list HEAD --not 'HEAD^1'); then
      echo "error: cannot list the commits merged by HEAD" >&2
      exit 1
    fi
  else
    range="HEAD"
    commits=$(git rev-parse HEAD)
  fi
else
  if ! git fetch --quiet origin '+main:refs/remotes/origin/main'; then
    echo "error: cannot fetch origin/main; commits not checked" >&2
    exit 1
  fi
  range="origin/main..HEAD"
  if ! commits=$(git rev-list origin/main..HEAD); then
    echo "error: cannot list commits in $range" >&2
    exit 1
  fi
fi

bad=()
added_bad=()
unchecked=()
for c in $commits; do
  short=$(git log -1 --format=%h "$c")
  msg=$(git log -1 --format=%B "$c")
  rc=0; scan "$msg" || rc=$?
  if [ "$rc" -eq 0 ]; then bad+=("$short"); elif [ "$rc" -ne 1 ]; then unchecked+=("$short (message)"); fi
  # Text the commit adds, even if a later commit removes it again: history keeps it.
  if ! added=$(added_lines "$c"); then
    unchecked+=("$short (diff)")
    continue
  fi
  rc=0; scan "$added" || rc=$?
  if [ "$rc" -eq 0 ]; then added_bad+=("$short"); elif [ "$rc" -ne 1 ]; then unchecked+=("$short (diff)"); fi
done
if [ ${#unchecked[@]} -gt 0 ]; then
  echo "error: ${#unchecked[@]} item(s) in $range could not be scanned; commit(s) not checked:" >&2
  printf '  %s\n' "${unchecked[@]}" >&2
  status=1
fi
if [ ${#bad[@]} -gt 0 ]; then
  echo "error: ${#bad[@]} commit message(s) in $range carry a session link, session trailer or Claude Code footer:" >&2
  printf '  %s\n' "${bad[@]}" >&2
  echo "Reword them before they are pushed (see AGENTS.md, Commits); for pushed commits, ask the owner." >&2
  status=1
else
  echo "ok: no session links or footers in commit messages ($range)"
fi
if [ ${#added_bad[@]} -gt 0 ]; then
  echo "error: ${#added_bad[@]} commit(s) in $range add a session link, session trailer or Claude Code footer to a file (even if a later commit removes it, history keeps it):" >&2
  printf '  %s\n' "${added_bad[@]}" >&2
  echo "Rewrite those commits before they are pushed; for pushed commits, ask the owner." >&2
  status=1
else
  echo "ok: no session links or footers in text added by commits ($range)"
fi

# 2. Pull request titles and descriptions: the description can end up in the squash body.
# CircleCI lists every PR that contains the build in CIRCLE_PULL_REQUESTS (comma
# separated); CIRCLE_PULL_REQUEST names only one of them. Check them all.
fetch_pr() {
  # Print the PR's JSON. The token goes to curl on stdin, never into argv.
  if [ -n "${GITHUB_TOKEN:-}" ]; then
    printf 'header = "Authorization: Bearer %s"\n' "$GITHUB_TOKEN" \
      | curl -fsSL --config - -H "Accept: application/vnd.github+json" "$1"
  else
    curl -fsSL -H "Accept: application/vnd.github+json" "$1"
  fi
}

prs="${CIRCLE_PULL_REQUESTS:-${CIRCLE_PULL_REQUEST:-}}"
if [ -n "$prs" ]; then
  IFS=',' read -ra pr_list <<< "$prs"
  for pr_url in "${pr_list[@]}"; do
    pr_url="${pr_url// /}"
    [ -n "$pr_url" ] || continue
    api="https://api.github.com/repos/${pr_url#https://github.com/}"
    api="${api/\/pull\//\/pulls\/}"
    text=""
    readable=0
    if json=$(fetch_pr "$api" 2>/dev/null); then
      if text=$(printf '%s' "$json" | python3 -c '
import json, sys
d = json.load(sys.stdin)
if not isinstance(d, dict) or "title" not in d:
    sys.exit(1)
print(d.get("title") or "")
print(d.get("body") or "")
' 2>/dev/null); then
        readable=1
      fi
    fi
    if [ "$readable" -eq 1 ]; then
      rc=0; scan "$text" || rc=$?
      if [ "$rc" -eq 0 ]; then
        echo "error: the title or description of $pr_url carries a session link or a Claude Code footer. Edit it, then re-run this job." >&2
        status=1
      elif [ "$rc" -eq 1 ]; then
        echo "ok: no session links or footers in the title or description of $pr_url"
      else
        echo "error: scanning the title and description of $pr_url failed; not checked" >&2
        status=1
      fi
    elif [ -n "${GITHUB_TOKEN:-}" ]; then
      echo "error: could not read $pr_url from the GitHub API with GITHUB_TOKEN set; its title and description were not checked" >&2
      status=1
    else
      echo "warning: could not read $pr_url from the GitHub API (no GITHUB_TOKEN set); its title and description were not checked" >&2
    fi
  done
else
  echo "note: no pull request for this build yet; no description checked"
fi

# 3. Tracked files (text only), in the final tree. git grep's own diagnostics go to a
# file: exit 1 with diagnostics (a read error) is a failed scan, not a clean one.
errs=$(mktemp)
trap 'rm -f "$errs"' EXIT
rc=0
files=$(git grep -lIiE "$PATTERN" 2>"$errs") || rc=$?
if [ "$rc" -eq 0 ]; then
  echo "error: tracked file(s) carry a session link, session trailer or Claude Code footer:" >&2
  printf '%s\n' "$files" | sed 's/^/  /' >&2
  status=1
elif [ "$rc" -eq 1 ] && [ ! -s "$errs" ]; then
  echo "ok: no session links or footers in tracked files"
else
  echo "error: git grep failed with exit $rc (or reported read errors); tracked files not checked" >&2
  status=1
fi

exit $status

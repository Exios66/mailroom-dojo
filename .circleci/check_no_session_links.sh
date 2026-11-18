#!/usr/bin/env bash
# Fails CI when a commit message, an open pull request's title or description, or a
# tracked file carries a link to a Claude session, project, thread, artifact or share
# page, a session trailer, or a Claude Code footer. Run by the CircleCI job
# `no-session-links` from the repository root.
#
#   On main:         the commit at HEAD (the squash-merge message) and every tracked file.
#   On other branches: every commit not yet on origin/main, every pull request CircleCI
#                    reports for the build, and every tracked file.
#
# It names offending commits, pull requests and files, and never prints the matched
# text: CI logs are public. The bracketed letters in PATTERN keep this file (and docs
# that quote the pattern) from matching itself. Tests: tools/governance/tests.
set -euo pipefail

PATTERN='https?://([[:alnum:]-]+\.)*claude\.a[i]|claude\.a[i]/[[:alnum:]]|claude\.co[m]/(claude-code|code|project|chat|artifact|share)|Claude-[S]ession[[:space:]]*:|G[e]nerated[[:space:]]+(with|by)[[:space:]]+\[?Claude[[:space:]]+Code'
status=0

# 1. Commit messages.
branch="${CIRCLE_BRANCH:-$(git rev-parse --abbrev-ref HEAD)}"
if [ "$branch" = "main" ]; then
  range="HEAD"
  if ! commits=$(git rev-parse --verify HEAD); then
    echo "error: cannot read HEAD; commits not checked" >&2
    exit 1
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
for c in $commits; do
  msg=$(git log -1 --format=%B "$c")
  # grep without -q reads all input, so the writer never dies of SIGPIPE under pipefail.
  if printf '%s\n' "$msg" | grep -iE "$PATTERN" >/dev/null; then
    bad+=("$(git log -1 --format=%h "$c")")
  fi
done
if [ ${#bad[@]} -gt 0 ]; then
  echo "error: ${#bad[@]} commit message(s) in $range carry a session link, session trailer or Claude Code footer:" >&2
  printf '  %s\n' "${bad[@]}" >&2
  echo "Reword them before they are pushed (see .gitmessage); for pushed commits, ask the owner." >&2
  status=1
else
  echo "ok: no session links or footers in commit messages ($range)"
fi

# 2. Pull request titles and descriptions: the description becomes the squash body.
# CircleCI lists every PR that contains the build in CIRCLE_PULL_REQUESTS (comma
# separated); CIRCLE_PULL_REQUEST names only one of them. Check them all.
prs="${CIRCLE_PULL_REQUESTS:-${CIRCLE_PULL_REQUEST:-}}"
if [ -n "$prs" ]; then
  auth=()
  if [ -n "${GITHUB_TOKEN:-}" ]; then auth=(-H "Authorization: Bearer $GITHUB_TOKEN"); fi
  IFS=',' read -ra pr_list <<< "$prs"
  for pr_url in "${pr_list[@]}"; do
    pr_url="${pr_url// /}"
    [ -n "$pr_url" ] || continue
    api="https://api.github.com/repos/${pr_url#https://github.com/}"
    api="${api/\/pull\//\/pulls\/}"
    if text=$(curl -fsSL ${auth[@]+"${auth[@]}"} -H "Accept: application/vnd.github+json" "$api" \
        | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d.get("title") or ""); print(d.get("body") or "")'); then
      if printf '%s\n' "$text" | grep -iE "$PATTERN" >/dev/null; then
        echo "error: the title or description of $pr_url carries a session link or a Claude Code footer. Edit it, then re-run this job." >&2
        status=1
      else
        echo "ok: no session links or footers in the title or description of $pr_url"
      fi
    else
      echo "warning: could not read $pr_url from the GitHub API; its title and description were not checked" >&2
    fi
  done
else
  echo "note: no pull request for this build yet; no description checked"
fi

# 3. Tracked files (text only).
rc=0
files=$(git grep -lIiE "$PATTERN") || rc=$?
if [ "$rc" -eq 0 ]; then
  echo "error: tracked file(s) carry a session link, session trailer or Claude Code footer:" >&2
  printf '%s\n' "$files" | sed 's/^/  /' >&2
  status=1
elif [ "$rc" -eq 1 ]; then
  echo "ok: no session links or footers in tracked files"
else
  echo "error: git grep failed with exit $rc; tracked files not checked" >&2
  status=1
fi

exit $status

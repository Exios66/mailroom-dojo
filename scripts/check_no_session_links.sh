#!/usr/bin/env bash
# Reject agent session links in tracked files and in commit messages.
#
# Same patterns as the CircleCI `no-session-links` job, which runs this script. Checks:
#   - every tracked file (git grep);
#   - the messages of the commits in BASE..HEAD (BASE defaults to origin/main);
#   - each MESSAGE_FILE given as an argument (the commit-msg hook passes the new message).
#
# Usage: check_no_session_links.sh [--require-base] [MESSAGE_FILE ...]
#   --require-base  fail when BASE does not resolve (CI); without it the commit-message range
#                   is skipped with a note (a fresh clone or an offline machine).
# Exit: 0 clean, 1 a link was found or a check could not run.
#
# The bracketed letters keep this file from matching its own patterns.
set -euo pipefail

pattern='claude\.ai/[c]ode|Claude-[S]ession:'
base="${BASE:-origin/main}"
require_base=0
if [ "${1:-}" = "--require-base" ]; then
  require_base=1
  shift
fi

status=0

for message_file in "$@"; do
  if grep -nE "$pattern" "$message_file"; then
    echo "The commit message contains a session link."
    status=1
  fi
done

if git rev-parse --verify --quiet "$base" >/dev/null; then
  log=$(git log --format='%H%n%B' "$base..HEAD")
  if printf '%s\n' "$log" | grep -nE "$pattern"; then
    echo "A commit message on this branch (since $base) contains a session link."
    status=1
  fi
elif [ "$require_base" -eq 1 ]; then
  echo "Base ref $base does not resolve, so commit messages cannot be checked."
  status=1
else
  echo "note: $base does not resolve; commit messages since it were not checked." >&2
fi

rc=0
git grep -nE "$pattern" || rc=$?
if [ "$rc" -eq 0 ]; then
  echo "A tracked file contains a session link."
  status=1
elif [ "$rc" -gt 1 ]; then
  echo "git grep failed with exit $rc."
  status=1
fi

exit "$status"

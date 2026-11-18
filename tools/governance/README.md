# tools/governance

Tests for the repository's own guard rails. They are not a package: nothing here is
installed or imported by `packages/`.

| What is tested | File under test |
|:---|:---|
| The session-link leak guard CI runs (commits, PR title and description, tracked files) | `.circleci/check_no_session_links.sh` |
| The PreToolUse hook that refuses edits to `.env` files, stored run data and script-written reports | `.claude/hooks/guard_paths.py` |
| The PreToolUse hook that refuses a Bash, edit or GitHub tool call carrying a session link, session trailer or Claude Code footer | `.claude/hooks/no_session_links.py` |

Run them with the standard library, git, bash and pytest only (no network, no package
install):

```bash
python -m pytest tools/governance/tests -q --import-mode=importlib
```

The guard tests build real git repositories under pytest's `tmp_path`, with a bare
repository on disk as `origin`, and replace `curl` with a stub on `PATH` for the PR
check. Forbidden strings in the tests are assembled from parts, so the repository
passes its own scan (`test_repository_passes_its_own_scan`). Until the CircleCI `test`
job runs this command, run it locally on every PR that touches these files.

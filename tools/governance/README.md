# tools/governance

Tests for the repository's own guard rails. They are not a package: nothing here is
installed or imported by `packages/`.

| What is tested | File under test |
|:---|:---|
| The session-link guard (commits, PR titles and descriptions, tracked files); CI runs it once the wiring follow-up in `AGENTS.md` section 12 lands | `.circleci/check_no_session_links.sh` |
| The PreToolUse hook that refuses edit-tool writes to `.env` files, stored run data and script-written reports | `.claude/hooks/guard_paths.py` |
| The PreToolUse hook that refuses a Bash, edit or GitHub tool call carrying a session link, session id or trailer, or a Claude Code footer | `.claude/hooks/no_session_links.py` |
| The hook wiring and the allow, ask and deny lists | `.claude/settings.json` |

Run them with the standard library, git, bash and pytest only (no network, no package
install):

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest tools/governance/tests -q --import-mode=importlib -p no:cacheprovider
```

The guard tests build real git repositories under pytest's `tmp_path`, with a bare
repository on disk as `origin`, and replace `curl` with a stub on `PATH` for the PR
check. Forbidden strings in the tests are assembled from parts, so the repository
passes its own scan (`test_repository_passes_its_own_scan`). The two environment
settings keep caches out of the tree until PR #2's `.gitignore` is on `main`. Until the
wiring follow-up in `AGENTS.md` section 12 adds this command to the CircleCI `test` job,
run it locally on every PR that touches these files.

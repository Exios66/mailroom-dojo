# tools/governance

Tests for the repository's own guard rails: the session-link guard and the agent hooks.
They are not a package: nothing here is installed or imported by `packages/`.

## Run the tests

You need Python 3.10 or later, the standard library, git, bash and pytest only (no network,
no package install). The suite and the hooks run `python3` from `PATH`, so that interpreter
must be 3.10 or later too. From the repository root:

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest tools/governance/tests -q --import-mode=importlib -p no:cacheprovider
```

The two environment settings keep bytecode and pytest caches out of the tree.

Until the wiring follow-up in `AGENTS.md` section 12 adds this command to the CircleCI
`test` job, run it locally on every PR that touches these files.

## What is tested

| What is tested | File under test |
|:---|:---|
| The session-link guard (commits, PR titles and descriptions, tracked files); CI runs it once the wiring follow-up in `AGENTS.md` section 12 lands | `.circleci/check_no_session_links.sh` |
| The PreToolUse hook that refuses edit-tool writes to `.env` files, stored run data and script-written reports | `.claude/hooks/guard_paths.py` |
| The PreToolUse hook that refuses a Bash, edit or GitHub tool call carrying a session link, session id or trailer, or a Claude Code footer | `.claude/hooks/no_session_links.py` |
| The hook wiring and the allow, ask and deny lists | `.claude/settings.json` |

The workspace's own, narrower check, `scripts/check_no_session_links.sh`
(`make session-links` and the CircleCI `no-session-links` job), is tested in
`tests/test_no_session_links.py`, not here.

## How the tests work

- The guard tests build real git repositories under pytest's `tmp_path`, with a bare
  repository on disk as `origin`.
- They replace `curl` with a stub on `PATH` for the PR check.
- Forbidden strings in the tests are assembled from parts, so the repository passes its
  own scan (`test_repository_passes_its_own_scan`).

## Read more

What each governance file is for, and what the settings and hooks do and do not enforce:
`AGENTS.md`, section 14. The session-link guard in detail: `AGENTS.md`, section 12.

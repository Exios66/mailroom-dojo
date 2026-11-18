# Contributing

This workspace replaces three older repositories with packages around one run record.
`CLAUDE.md` is the short version of the rules, `AGENTS.md` the manual, and
`.claude/rules/` the binding detail. This page is the pull request guidance.

## Before you open a PR

1. **One concern per PR.** A package feature, a fix, a docs change or a stage gate, not a
   mix.
2. **Commit in the `.gitmessage` shape** (added by PR #2): `type(scope): imperative
   summary`, a blank line, then `Why:`, `What changed:`, `Testing: <command> -> <summary
   line>` and `Refs:`. A `Co-Authored-By` trailer is fine.
3. **Vet it for bugs.** Every behaviour change has a test; every bug fix has a test that
   fails before the fix and passes after. Tests must be able to detect failure: injected
   faults, property and stateful tests where code touches disk, processes or concurrency,
   and one deliberate mutation shown to turn a test red. Tests use `tmp_path`, never a
   shared `/tmp` path, and must pass in any order and under `pytest-xdist` (`-n auto`).
   CI currently runs them serially; a follow-up adds `-n auto`.
4. **Run the local gate on the final commit, on a clean tree**: each touched package's
   suite, `PYTHONDONTWRITEBYTECODE=1 python -m pytest tools/governance/tests -q
   --import-mode=importlib -p no:cacheprovider` (no caches in the tree until a
   `.gitignore` lands), and `bash .circleci/check_no_session_links.sh`. Never skip, loosen or disable a test to get
   green; reproduce a CI failure locally before fixing it.
5. **Get one independent review.** A fresh-context Opus reviewer reads the final diff and
   the draft PR description once, before the PR is pushed; that review also covers
   anything a cheaper model produced. Fixes are verified with red-then-green tests and
   the gates re-run, not re-reviewed (`.claude/rules/orchestration.md`).

## Writing the PR

- **Open it as a draft by default, and mark it ready when the gate is met**: every
  evidence item present and CircleCI green on the pushed commit
  (`.claude/rules/pr-flow.md`, step 4). Open it ready for review only when the owner
  asks; the merge gate applies either way.
- Use the default template, or a named one in `.github/PULL_REQUEST_TEMPLATE/`
  (`docs.md` for specs, plans and ADRs; `stage-gate.md` for a stage gate). Add
  `?template=docs.md` to the new-PR URL to pick one.
- Write an **executive summary, never a transcript**: no direct quotes of the requester,
  no names, handles, credentials or local paths (except the one-line attribution comment,
  which carries only the login), and no document text. This repository is public.
- Fill in Evidence with the commit sha and raw output, Not verified, the Review log (with
  the model and effort of each review) and Cross-repo impact ("none" needs a reason).
- **No session links, ever.** No claude.ai link, session trailer or Claude Code footer in
  a commit, PR title, PR body, comment, tag or release note. In cloud sessions the PR
  creation and comment tools append one on the server side, where no hook sees it, so
  keep the body in a scratch file, overwrite the body from it right after creating the PR
  (MCP `update_pull_request`), read it back, and do not post comments from those
  sessions. `bash .circleci/check_no_session_links.sh` checks commits, PR text and
  tracked files; once the wiring follow-up in `AGENTS.md` section 12 lands, CircleCI's
  `no-session-links` job runs it on every build (re-run it after editing a PR body). A local run checks PR text only when you name the PR: `CIRCLE_PULL_REQUEST=https://github.com/<owner>/<repo>/pull/<n> bash .circleci/check_no_session_links.sh`. Without it the script prints "no pull request for this build yet" and exits 0, which proves nothing about the PR title or body.

## Merging

- CircleCI is the only CI (`test` and `no-session-links`). GitHub Actions are not used.
  Until the wiring follow-up lands, those jobs do not run the guard script or the
  governance tests; the local runs above are the evidence.
  No linter or type checker is configured yet: say "not configured" rather than claiming
  one passed.
- **Interim:** until `.circleci/config.yml` is on `main`, no CircleCI job reports. Then
  "both jobs green" is replaced by the local runs of the governance suite and the guard
  script on the verified head, with exit codes quoted in the PR, and the owner's go-ahead
  must name this exception (`AGENTS.md`, section 12). The governance PR merges
  independently of PRs #1 to #3 (no overlapping files); the wiring follow-up comes after.
- Merge only when both jobs are green on the verified head, every review thread is
  handled, the one independent review has run and its fixes are verified, a final sweep
  for session links is clean, and the owner has given the go-ahead (unless the owner has durably said otherwise).
- **Squash-merge** with a hand-written title (`type(scope): summary (#N)`) and body (what
  and why, `Fixes #N`, optional `Co-Authored-By`). Never accept GitHub's concatenated
  default or the PR body as the message. End the title with `(#N)` yourself (GitHub adds
  no suffix when a title is given). Pin the verified head sha. Always squash; if a merge
  commit ever seems needed, ask the owner.
- Never force-push or rewrite pushed history. For a stacked PR, retarget it to `main`
  after its base merges, merge `origin/main` into it and re-run the gates
  (`.claude/rules/pr-flow.md`, step 4).

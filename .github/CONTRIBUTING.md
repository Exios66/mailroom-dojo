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
   shared `/tmp` path, because CI runs them in parallel workers.
4. **Run the local gate on the final commit, on a clean tree**: each touched package's
   suite, `python -m pytest tools/governance/tests -q --import-mode=importlib`, and
   `bash .circleci/check_no_session_links.sh`. Never skip, loosen or disable a test to get
   green; reproduce a CI failure locally before fixing it.
5. **Get an independent review.** A fresh-context Opus reviewer reads the final diff before
   the PR is pushed; anything a cheaper model produced gets its own Opus review.

## Writing the PR

- Use the default template, or a named one in `.github/PULL_REQUEST_TEMPLATE/`
  (`docs.md` for specs, plans and ADRs; `stage-gate.md` for a stage gate). Add
  `?template=docs.md` to the new-PR URL to pick one.
- Write an **executive summary, never a transcript**: no direct quotes of the requester,
  no names, handles, credentials or local paths, and no document text. This repository
  is public.
- Fill in Evidence with the commit sha and raw output, Not verified, the Review log (with
  the model and effort of each review) and Cross-repo impact ("none" needs a reason).
- **No session links, ever.** No claude.ai link, session trailer or Claude Code footer in
  a commit, PR title, PR body, comment, tag or release note. In cloud sessions the PR
  creation and comment tools append one, so overwrite the body right after creating the
  PR (MCP `update_pull_request`), read it back, and do not post comments from those
  sessions. CircleCI's `no-session-links` job fails the build otherwise.

## Merging

- CircleCI is the only CI (`test` and `no-session-links`). GitHub Actions are not used.
  No linter or type checker is configured yet: say "not configured" rather than claiming
  one passed.
- Merge only when both jobs are green on the verified head, every review thread is
  handled, a final independent review has run, a final sweep for session links is clean,
  and the owner has given the go-ahead.
- **Squash-merge** with a hand-written title (`type(scope): summary (#N)`) and body (what
  and why, `Fixes #N`, optional `Co-Authored-By`). Never accept GitHub's concatenated
  default. Pin the verified head sha. Branch-sync PRs may use a merge commit.
- Never force-push or rewrite pushed history. Prefer retargeting a PR's base.

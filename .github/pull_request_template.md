<!--
An executive summary, never a transcript: no direct quotes of the requester, no names,
handles, credentials or local paths. Never add a claude.ai link, a session trailer or a
tool footer anywhere in this description (.claude/rules/pr-flow.md, step 0). For docs or
stage-gate PRs, use a named template in .github/PULL_REQUEST_TEMPLATE/.
-->

## Problem

<!-- The issue, plan task or requirement (R1-R10) this PR answers, in your own words. Link the issue. -->

## Change

<!-- What changed and why this approach. One concern per PR. Name the packages touched. -->

## Evidence

<!--
Proof that the change solves the problem above, taken on the final commit on a clean tree.
See .claude/rules/evidence-contract.md. Quote real output; trim with … ; long output in <details>.
-->

Commit: `<short sha>`

- **Tests:** `<each touched package's suite>` -> `<summary line>`
- **Governance guards:** `PYTHONDONTWRITEBYTECODE=1 python -m pytest tools/governance/tests -q --import-mode=importlib -p no:cacheprovider` -> `<summary line>`
- **Regression test (bug fixes):** `<test name>` fails before, passes after
- **Failure detection (R8):** `<the deliberate mutation or injected fault, and the test that went red>`
- **Lint and types:** not configured in this repository
- **Session links:** `CIRCLE_PULL_REQUEST=<this PR's URL> bash .circleci/check_no_session_links.sh` -> `<exit code>`; title and body read back after the last edit

<details><summary>Raw output</summary>

```text
```

</details>

## Not verified

<!-- What you could not check and why (paid or real-model paths, network-only checks, surfaces you could not render). "Nothing" is a claim too. -->

## Review log

<!-- One review per PR; its fixes are verified, not re-reviewed. Name the model tier and effort. See .claude/rules/orchestration.md. The review also counts as the Opus review of any Sonnet or Haiku output. -->

- Review, fresh context (`pre-pr-reviewer` or `adversarial-reviewer`, `<model, effort>`, on `<sha>`): `<READY / FIX FIRST, or ACCEPT / ACCEPT WITH FIXES / REJECT>`, `<N>` findings (`<B>` BLOCKING, `<M>` MAJOR, `<m>` MINOR)
- Fixes verified by the orchestrator: `<finding -> fix commit, red-then-green test (or, for a docs fix, the probe re-run), gates re-run on <sha>>` or `none needed`
- Second review: `<not needed / reason (design change or more than about 150 lines), model, effort, result>`

## Risks, rollback and follow-ups

<!-- What could go wrong, how to undo it (usually: revert the squash commit), and any follow-up PR or issue. Short. -->

## Cross-repo and downstream impact

<!-- llm-mailroom, local-mailroom-sandbox, llm-dojo-scoring and their users keep shipping until cutover. Does anything here change what they must do? "None" and why. -->

## Squash-merge message

<!-- Hand-written. Title: type(scope): summary (#N). Body: what and why, Fixes #N, optional Co-Authored-By. Nothing else. -->

```text
```

## Checklist

- [ ] Evidence above was taken on the final commit, on a clean tree
- [ ] Every behaviour change has a test; every bug fix has a red-then-green regression test
- [ ] The new tests can fail (a deliberate mutation or injected fault was shown to turn one red)
- [ ] No test was skipped, deleted or loosened to get green; tests use `tmp_path`, not shared `/tmp` paths
- [ ] Package boundaries hold; tuning lives in `config/` or `labels/`, not in code
- [ ] Docs, `AGENTS.md`, `CLAUDE.md` and package READMEs match; every path, count and command named exists
- [ ] No real model call, paid run or hosted write without the owner's go-ahead
- [ ] No session links or Claude Code footers in commits or this description
- [ ] Squash-merge message drafted above

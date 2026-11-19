# Evidence contract

**Nothing is "done", "fixed", "passing" or "working" until you hold evidence for it.**
This applies to the orchestrator, every subagent report, commit messages and PR
descriptions. A claim without evidence is a guess: label it as one or drop it.

This repository will define what "correct" means for the Mailroom (the Dojo scores every
run), so a wrong number here becomes a wrong number in every report built on it.

## Rules

1. **Evidence is output, not prose.** Quote the command, its exit code, the summary line
   and the commit sha. "Should work", "looks right" and "I believe" are not evidence.
2. **Evidence comes from the final state.** Commit the last edit (local commits need no
   request), check that `git status --short` prints nothing, name the commit
   (`git rev-parse --short HEAD`) and run the gates there. Output from before a later
   change is stale.
3. **Prove the original problem is solved, not just that nothing broke.** For a bug, a
   test that fails before the fix and passes after (red, then green). For a feature, show
   it doing what was asked. For a new test, show it can fail: break the code on purpose
   (a deliberate mutation or an injected fault), watch the test go red, then restore.
4. **Say what you did not verify, and why.** Skipped tests, network-only checks, a paid or
   real-model path you were not allowed to run, a surface you could not render. Never
   present one package's suite, one file or one `-k` filter as the full suite.
5. **Re-verify subagent evidence.** The orchestrator re-runs at least the gates itself, or
   reads a `runner`'s raw output, before repeating any subagent claim.
6. **Never fabricate or tidy output.** Trimming is fine (mark it with `…`). Editing
   numbers, dropping failures or merging runs is not.

## What each kind of claim needs

| Claim | Evidence |
|:---|:---|
| Tests pass | Each touched package's documented suite, for example `.venv/bin/python -m pytest packages/record/tests -q` (after PR #2 merges): summary line (`N passed, M skipped …`), exit code, commit. Name every skip. Tests must pass in any order and under `pytest-xdist` (`-n auto`), so a test that passes only alone is not passing. CI currently runs serially; the wiring follow-up adds `-n auto`. |
| Governance guards pass | `PYTHONDONTWRITEBYTECODE=1 python -m pytest tools/governance/tests -q --import-mode=importlib -p no:cacheprovider` summary line (the two settings keep caches out of the tree until PR #2's `.gitignore` is on `main`), and `bash -n .circleci/check_no_session_links.sh` exit 0. |
| Bug fixed | The regression test, its failing output on the old code, its passing output on the new code. |
| Tests detect failure (R8) | The fault, property or stateful test, and one deliberate mutation or injected fault that turns it red, with that output. |
| Lint, types and spelling | `make lint`, `make typecheck` and `make spell`: each exit code and last line, on the final commit. mypy `--strict` covers only the packages in `[tool.mypy] files`; never claim "type-safe" for a package it does not cover. |
| Package boundaries hold | The package's boundary test (for the record package, `packages/record/tests/test_boundaries.py`, after PR #2 merges) passes. |
| Docs are true | Every path, count and command named exists, was run (paste the result) or is marked unverified. |
| Stage gate met | Numbers from the stored runs, produced by the gate script, with the method, the sample size and the commit; the report under `docs/reports/` is written by that script, never by hand. |
| No session links | `CIRCLE_PULL_REQUEST=<the PR's URL> bash .circleci/check_no_session_links.sh` on the final commit (exit 0; without the variable the PR text is not checked) and the PR title and body read back after the last edit. Once the wiring follow-up in `AGENTS.md` section 12 lands, also the CircleCI `no-session-links` job green on the head, re-run after the last body edit; until then that job runs a narrower inline check and is not this evidence. |
| Config or policy change | The run spec or policy hash before and after, and the tests or mock run that read it. |
| Cross-repo impact | Which older repo or downstream user is affected and whether it needs a follow-up. "None" is a claim too: say why. |

## Where evidence goes

- **Text** goes in the PR description under "Evidence", inside `<details>` blocks for
  anything longer than ten lines (`.github/pull_request_template.md`). Before pasting,
  remove tokens, keys, local paths and any document text.
- **Screenshots and recordings** are saved under `.evidence/` and never committed to a
  feature branch. Attach them to the PR by hand, or push them to an evidence branch only
  when the owner has agreed to one.
- **Captures are synthetic.** This repository is public. Evidence contains only synthetic
  or sample data, never real documents, client names, people's names, tokens, credentials
  or local paths. If a surface cannot be shown that way, leave it out and say so under
  "Not verified".
- **The final report to the owner** carries the same evidence, short, and leads with what
  was verified and what was not.

<!--
Stage-gate PR: the evidence that a project passed (or did not) its gate, as its plan in
docs/plans/ defines it. An executive summary, never a transcript: no direct quotes,
names, handles, credentials, local paths or document text, and no claude.ai link, session
trailer or tool footer anywhere (.claude/rules/pr-flow.md).
-->

## Project and gate

<!-- "Project N's gate, from its plan": name the project and link its plan in docs/plans/. For each gate criterion, one row below with a short summary in your own words and a link to the plan section; no quotes and no document text. Do not reuse the design's stage numbers. -->

| Criterion (summary, linked to the plan) | Requirement | Result | Pass |
|:---|:---|:---|:---|
| `<criterion>` | `<R#>` | `<number or outcome>` | yes / no |

## Numbers before and after

<!-- From the stored runs, produced by the gate script, never typed by hand. Same draw on both sides. -->

| Measure | Before (old stack or last gate) | After | Method and sample size |
|:---|:---|:---|:---|
| `<measure>` | | | |

Commit: `<short sha>`
Gate script: `<command>` -> `<exit code>`
Report: `docs/reports/<kind>/<file>` (written by the script)
Session links: `CIRCLE_PULL_REQUEST=<this PR's URL> bash .circleci/check_no_session_links.sh` -> `<exit code>`; title and body read back after the last edit

<details><summary>Raw gate output</summary>

```text
```

</details>

## Go or no-go

<!-- GO, NO-GO or GO WITH CONDITIONS, and the one-paragraph reason. -->

## Decision needed

<!-- Each decision as a question with the options and the one you recommend, with why. Intended differences that need sign-off are listed here, one per line. Ask the owner once (one decision card or message) and do not merge or start the next project until answered. "None" if nothing is open. -->

-

## Not verified

<!-- Paid parity runs not yet started by the owner, missing stored runs, anything else. -->

## Review log

- Review, fresh context (`pre-pr-reviewer` or `adversarial-reviewer`, `<model, effort>`, on `<sha>`): `<READY / FIX FIRST, or ACCEPT / ACCEPT WITH FIXES / REJECT>`, `<N>` findings (`<B>` BLOCKING, `<M>` MAJOR, `<m>` MINOR)
- Fixes verified by the orchestrator: `<finding -> fix commit, red-then-green test (or, for a docs fix, the probe re-run), gates re-run on <sha>>` or `none needed`
- Second review: `<not needed / reason (design change or more than about 150 lines), model, effort, result>`

## Risks, rollback and follow-ups

<!-- What could go wrong, how to undo it (usually: revert the squash commit), and any follow-up PR or issue. Short. -->

## Cross-repo and downstream impact

<!-- What changes for the older repos and their users if this gate passes. "None" and why. -->

## Squash-merge message

```text
```

## Checklist

- [ ] Every number comes from the stored runs through the gate script, with its method and sample size
- [ ] The report under `docs/reports/` was written by the script, not by hand
- [ ] No document text, client names or tokens in the numbers, report or this description
- [ ] Every paid run named was started by the owner
- [ ] No session links or Claude Code footers in commits or this description
- [ ] Squash-merge message drafted above

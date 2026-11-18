<!--
Stage-gate PR: the evidence that a project stage passed (or did not) its gate from the
design and the stage's plan. An executive summary, never a transcript: no direct quotes,
names, handles, credentials, local paths or document text, and no claude.ai link, session
trailer or tool footer anywhere (.claude/rules/pr-flow.md).
-->

## Stage and gate

<!-- The stage (for example "1. Record and store"), its plan in docs/plans/, and the gate criteria quoted from the plan, one per row below. -->

| Criterion (from the plan) | Requirement | Result | Pass |
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

<details><summary>Raw gate output</summary>

```text
```

</details>

## Go or no-go

<!-- GO, NO-GO or GO WITH CONDITIONS, and the one-paragraph reason. -->

## What the owner must decide

<!-- Each decision as a question with the options and the recommended one. Intended differences that need sign-off are listed here, one per line. -->

-

## Not verified

<!-- Paid parity runs not yet started by the owner, missing stored runs, anything else. -->

## Review log

- Adversarial rounds (`<lens>`, `<model, effort>`): `<findings and fixes>`
- Pre-PR review, fresh context (`<model, effort>`): `<READY / fixes applied>`

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

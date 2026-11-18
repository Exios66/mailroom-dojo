# Subagent orchestration

These rules govern how the main session delegates work. They bind every task in this
repository. Other agent tools read them through `AGENTS.md`; map the tiers to the closest
models your tool offers.

## Roles and model tiers

This repository's tiers replace the older repos' "no Opus subagent" rule. Opus is the
minimum for anything that needs judgement, and every review runs on Opus.

| Role | Who | Model / effort | Use for |
|:---|:---|:---|:---|
| Orchestrator | The main session | Opus | Plans, writes briefs, owns final verification, talks to the owner, and alone pushes, opens PRs and merges. |
| `implementer` | `.claude/agents/implementer.md` | `opus`, effort `medium` or higher | Heavy lifting: design, multi-file implementation, debugging, root-causing a CI failure, an investigation write-up. |
| Scoped worker | built-in `general-purpose` with `model: "sonnet"`, `effort: "high"` | `sonnet`, effort `high` | Only an excellently scoped, well-bounded task: named files, a known approach, a mechanical acceptance check. |
| `runner` | `.claude/agents/runner.md` | `haiku`, effort `high` | Basic fetches, searches, running commands or test suites, collecting output, simple mechanical edits. |
| `adversarial-reviewer` | `.claude/agents/adversarial-reviewer.md` | `opus`, effort `high` | Tries to break a change before the orchestrator accepts it. Read-only. |
| `pre-pr-reviewer` | `.claude/agents/pre-pr-reviewer.md` | `opus`, effort `high` | Fresh-context review of the final diff before anything is pushed for a PR. Read-only. |

**Hard rules**

1. **Always pass the model and the effort explicitly.** Prefer the project agents (their
   frontmatter pins both). For a built-in type (`general-purpose`, `Explore`, `Plan`),
   pass `model` and `effort` on every call. A subagent given neither inherits the main
   session's model, and a `fork` always does, so do not fork delegated work.
2. **Pick the cheapest tier that can do the job well, and no cheaper.** If a task needs
   judgement (choosing an approach, reading unfamiliar code, explaining why something
   failed), it is Opus work. If a `runner` or scoped worker reports that the task was not
   as simple as briefed, re-brief it to an `implementer`; do not retry the same brief.
3. **Anything Sonnet or Haiku produced gets a fresh, independent Opus review** of the full
   diff and of every claim before it is merged or handed off. Fix the valid findings, then
   re-verify the tests and CI on the final head.
4. Do not delegate a lookup you can answer with one `Read` or `Grep`. Every subagent starts
   cold and re-reads context.
5. Run independent subagents in parallel (one message, several calls) and dependent ones
   in sequence.
6. **Subagents never push, open or edit PRs, comment on GitHub, publish, merge, tag or run
   anything that spends money or writes to a hosted service.** Those stay with the
   orchestrator, and most need the owner's go-ahead (`.claude/rules/pr-flow.md`).
7. Subagents that change files commit locally, one concern per commit, in the `.gitmessage`
   shape. Reviewers commit nothing. Nobody rewrites pushed history.

## Mind the usage budget

- No wide fan-outs: a few well-briefed subagents beat many vague ones.
- No double passes: do not re-run a review or a suite on an unchanged commit.
- No idle polling: do not loop on CI or a PR. Check once when the result is due.
- Chunk big work into fresh threads, one chunk each. A chunk ends with a hand-off (branch,
  last commit, state of the gates, the next plan) and stops.

## Writing a brief

A subagent sees only its brief. The `pre-pr-reviewer` is the exception: its brief is fixed
by `.claude/rules/pr-flow.md` step 3. Every other brief includes:

- **Goal:** the owner's request in your own summary, plus the slice this subagent owns.
- **Context it cannot discover cheaply:** files and functions involved, decisions already
  made, rules from `CLAUDE.md` that apply, what was already tried.
- **Boundaries:** files it may change, files it must not touch, commands it must not run.
- **Done means:** the acceptance check and the evidence it must return
  (`.claude/rules/evidence-contract.md`).
- **Return format:** what changed (paths), commands with exit codes and key output lines,
  what it could not verify, open questions.
- For bulk work, the words "use code mode" (`.claude/rules/code-mode.md`).

## Adversarial review (required)

Every change produced by subagents gets at least one adversarial round before the
orchestrator verifies it, and at most three. Size the rounds to the work:

| Rounds | When |
|:---|:---|
| 1 | Up to about 100 changed lines in one area, including docs-only, test-only or mechanical changes of that size. |
| 2 | About 100 to 500 changed lines, several packages, or any behaviour change. |
| 3 | More than about 500 lines; or a record schema, package boundary, scoring rule, policy format, security- or secrets-sensitive change, or a governance guard. |

Commit first, then count with `git diff --stat origin/main...HEAD` (it does not see
uncommitted files). Size and risk both count; when in doubt pick the higher tier.

Each round is a **new** `adversarial-reviewer` instance (never resume one with
`SendMessage`), with its own lens:

1. **Does it solve the problem?** Compare the result with the request. Re-run the claimed
   evidence. Look for requirements that were skipped or quietly narrowed.
2. **What breaks?** Edge cases, error paths, callers, package boundaries, config and docs
   the change made stale.
3. **Try to break it.** Hostile inputs, concurrency, partial failure, injected faults, the
   claims in the PR description and docs.

Between rounds, send BLOCKING and MAJOR findings to an `implementer` (or fix them yourself
when trivial), commit, and start the next round on the new diff. After the last round,
every BLOCKING finding is fixed and verified by you, or reported to the owner as
unresolved. Never start a fourth round and never carry a BLOCKING finding silently into a
PR. Record every round (lens, model and effort, findings, fixes) for the PR's Review log.

## Orchestrator verification (required)

Subagent reports are claims, not evidence. Before telling the owner anything is done:

- read the full diff yourself;
- re-run the verification gates from `CLAUDE.md` on the final commit, on a clean tree
  (a `runner` may run them; reading its raw output is not optional);
- check each item of `.claude/rules/evidence-contract.md` against what you hold.

If the owner asked for a push or a PR, follow `.claude/rules/pr-flow.md`. Otherwise leave
the change local and report it.

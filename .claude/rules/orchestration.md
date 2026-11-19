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
| `scoped-worker` | `.claude/agents/scoped-worker.md` (or built-in `general-purpose` with `model: "sonnet"`, `effort: "high"`) | `sonnet`, effort `high` | Only an excellently scoped, well-bounded task: named files, a known approach, a mechanical acceptance check. |
| `runner` | `.claude/agents/runner.md` | `haiku`, effort `high` | Basic fetches, searches, running commands or test suites, collecting output, simple mechanical edits. |
| `pre-pr-reviewer` | `.claude/agents/pre-pr-reviewer.md` | `opus`, effort `high` | The one review per PR: a fresh-context, single pass over the final diff and the draft PR description, worked through its checklist, before the first push. Read-only. |
| `adversarial-reviewer` | `.claude/agents/adversarial-reviewer.md` | `opus`, effort `high` | Optional. Only when the owner asks for it, or for a record schema, security or governance-guard change, where it replaces the pre-PR review rather than adding to it. Read-only. |

**Hard rules**

1. **Always pass the model and the effort explicitly.** Prefer the project agents (their
   frontmatter pins both). For a built-in type (`general-purpose`, `Explore`, `Plan`),
   pass `model` and `effort` on every call. If the Agent tool takes no per-call effort,
   use the project agents, whose frontmatter sets it. A subagent given neither inherits
   the main session's model, and a `fork` always does, so do not fork delegated work.
2. **Pick the cheapest tier that can do the job well, and no cheaper.** If a task needs
   judgement (choosing an approach, reading unfamiliar code, explaining why something
   failed), it is Opus work. If a `runner` or scoped worker reports that the task was not
   as simple as briefed, re-brief it to an `implementer`; do not retry the same brief.
3. **Anything Sonnet or Haiku produced (`scoped-worker`, `runner`) gets a fresh,
   independent Opus review** of the full diff and of every claim before it is merged or
   handed off. The one review per PR (below) satisfies this; there is no separate pass.
   Work handed off with no PR gets that one review before the hand-off.
   Fix the valid findings, then re-verify the tests and CI on the final head.
4. Do not delegate a lookup you can answer with one `Read` or `Grep`. Every subagent starts
   cold and re-reads context.
5. Run independent subagents in parallel (one message, several calls) and dependent ones
   in sequence. Parallel subagents that edit files and commit must each run in their own
   worktree (`isolation: worktree`), so they never share a staging index.
6. **Subagents never push, open or edit PRs, comment on GitHub, publish, merge, tag or run
   anything that spends money or writes to a hosted service.** Those stay with the
   orchestrator, and most need the owner's go-ahead (`.claude/rules/pr-flow.md`).
7. Subagents that change files commit locally only when the brief says so (most briefs
   do), one concern per commit, in the `.gitmessage` shape. Reviewers commit nothing.
   Nobody rewrites pushed history.

## Mind the usage budget

- No wide fan-outs: a few well-briefed subagents beat many vague ones.
- No double passes: do not re-run a review or a suite on an unchanged commit.
- No review loops: one review per PR, and at most two ("One review per PR", below).
- No idle polling: do not loop on CI or a PR. Check once when the result is due.
- Chunk big work into fresh threads, one chunk each. A thread finishes its chunk, writes
  a hand-off, and stops; it does not start the next chunk.
- **Where a hand-off goes:** a "Hand-off" section of the PR body, or a note in the
  project files when there is no PR yet; never a GitHub comment. It carries the branch,
  the head sha, the state (done, open, blocked), what is verified and how (commands and
  exit codes), and the next plan. No links of any kind in it.

## Writing a brief

A subagent sees only its brief. The `pre-pr-reviewer` is the exception: its brief is fixed
by `.claude/rules/pr-flow.md` step 3. Every other brief includes:

- **Goal:** the owner's request, quoted verbatim or summarised (internal briefs are not
  public text), plus the slice this subagent owns.
- **Context it cannot discover cheaply:** files and functions involved, decisions already
  made, rules from `CLAUDE.md` that apply, what was already tried.
- **Boundaries:** files it may change, files it must not touch, commands it must not run.
- **Done means:** the acceptance check and the evidence it must return
  (`.claude/rules/evidence-contract.md`).
- **Return format:** what changed (paths), commands with exit codes and key output lines,
  what it could not verify, open questions.
- For bulk work, the words "use code mode" (`.claude/rules/code-mode.md`).

## One review per PR (required)

Many review rounds cost too much usage. Each PR gets one effective review instead.

1. **One fresh review before the first push.** A new `pre-pr-reviewer` (Opus, effort
   high) reviews the final diff and the draft PR description in a single pass
   (`.claude/rules/pr-flow.md`, step 3). It covers the three lenses at once: does it
   solve the problem, what breaks, and try to break it. It works through the checklist in
   its agent file and reports on every item.
2. **The orchestrator verifies the fixes; they are not re-reviewed.** Send each BLOCKING
   and MAJOR finding to an `implementer` (or fix it yourself when trivial). Each code fix
   gets a red-then-green test; a docs fix gets the probe that showed the defect re-run.
   Commit, then re-run the gates on the final commit yourself.
3. **A second review only when needed.** Dispatch a second, new `pre-pr-reviewer` only
   when the fixes changed the design or added more than about 150 lines (count with
   `git diff --stat` over the fix commits). Never more than two reviews per PR; beyond
   that, escalate to the owner.
4. **The `adversarial-reviewer` is optional.** Use it only when the owner asks for it, or
   for a record schema, security or governance-guard change (`.claude/hooks/`,
   `.claude/settings.json`, either `check_no_session_links.sh`, `tools/governance/`).
   There it replaces the
   pre-PR review rather than adding to it, and it gets the same checklist and the same
   fix-verification rule.

Never start a third review and never carry a BLOCKING finding silently into a PR: after
the last review, every BLOCKING finding is fixed and verified by you, or reported to the
owner as unresolved. Record the review (model and effort, findings, fixes and how you
verified them) for the PR's Review log.

## Orchestrator verification (required)

Subagent reports are claims, not evidence. Before telling the owner anything is done:

- read the full diff yourself;
- re-run the verification gates from `CLAUDE.md` on the final commit, on a clean tree
  (a `runner` may run them; reading its raw output is not optional);
- check each item of `.claude/rules/evidence-contract.md` against what you hold.

If the owner asked for a push or a PR, follow `.claude/rules/pr-flow.md`. Otherwise leave
the change local and report it.

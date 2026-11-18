# AGENTS.md: mailroom-dojo manual

The long-form manual for any agent tool (Claude Code, Cursor, Codex, OpenCode and others)
working in this repository. `CLAUDE.md` is the short version that Claude Code loads every
session; the binding rules are in `.claude/rules/`. Read the section you need, when you
need it.

## Contents

1. Start here
2. Repository layout
3. Package boundaries
4. Configuration, not code
5. Prompts, models and traces
6. Testing
7. Commits
8. Pull requests and merging
9. Review tiers
10. Evidence
11. Hosted, paid and real-model work
12. Continuous integration
13. Cross-repo rules
14. Governance files

## 1. Start here

1. Read `CLAUDE.md` and the four files in `.claude/rules/`. Tools other than Claude Code
   do not load them automatically; they bind you all the same.
2. Read the design, `docs/specs/2026-11-18-lean-rewrite-design.md` (the master plan),
   especially "Requirements added 2026-11-18" (R1 to R10), "Constraints" and "Build order
   and proof gates". Then read the plan for the project you are working on in
   `docs/plans/` and its test strategy (`docs/test-strategy-<package>.md`). These arrive
   with PR #2.
3. Check the actual state before trusting any doc: `git status --short`,
   `git log --oneline -5`, and the files you are about to change. If a doc and the code
   disagree, the code is the fact; fix the doc in the same PR or report it.

## 2. Repository layout

The layout map lives in `README.md` and in the design (R9). In short:

| Path | What lives there |
|:---|:---|
| `packages/<name>/` | One installable package each: `record` (project 1), `labels` (2), `dojo` (3), `engine` (5), `launcher` (6). Each has `pyproject.toml`, `README.md`, `src/<module>/`, `tests/`, `scripts/`. |
| `config/` | Everything tuned without code: client profiles, routing policies, prompt sets (from project 2 on). |
| `labels/<snapshot>/` | Committed, versioned label snapshots with manifests (project 2). |
| `docs/` | `specs/` (the design), `plans/` (one per project), `adr/`, `test-strategy-*.md`, `reports/` (written by scripts). |
| `.circleci/` | CI (CircleCI only) and the session-link guard. |
| `.claude/`, `.github/`, `tools/governance/` | Agent rules and hooks, PR templates, and the governance tests (section 14). |

Rules from R9: names say what a thing is, in full words; nesting goes from general to
specific; **a new folder needs a line in the map**. Folders for later projects do not
exist yet; do not create empty ones.

## 3. Package boundaries

Dependencies point one way, and a test in each package enforces it:

- `record` imports nothing of ours.
- `labels` imports `record`.
- `dojo` imports `record` and `labels`, and **never** imports `engine`.
- `engine` imports `record` and reads client profiles from `config/`.
- `launcher` may import all of them; it is the only package that composes the others.

The record package's boundary test parses every module with `ast` and fails on an import
of a sibling package (`packages/record/tests/test_boundaries.py`, after PR #2 merges). A
new package copies that test with its own forbidden list. The engine and launcher write
records; the Dojo only reads records and label files. That is what keeps the Dojo
installable alone and stops the old multi-repo dependency mess from growing back: no
commit pins between packages, no vendored copies, one version, one CI.

The record is domain-free (R4): it stores node names, class labels, decision payloads and
blob roles as data. No document class, node or client is hard-coded in it.

### Adding a package

1. Check the design names it and the project's plan exists. A package that is not in the
   map needs the owner's agreement and a map line first.
2. Create `packages/<name>/` with `pyproject.toml` (Python 3.11 or later, a small core
   dependency list, heavy or numerical dependencies behind an optional extra, a `dev`
   extra with `pytest`, `pytest-xdist` and `hypothesis`), `README.md` (purpose, install,
   test command), `src/mailroom_<name>/`, `tests/` and `scripts/`.
3. Add its boundary test, its test strategy (`docs/test-strategy-<name>.md`) and its line
   in the README map.
4. Add its suite to the CircleCI `test` job in the same PR.

## 4. Configuration, not code

Tuning is configuration and data, versioned in this repository and hashed into each run
(R4, R9, R10). A goal change is a config change and a measured run, never a code fork.
These directories arrive with project 2 and later; the shapes below are what the design
fixes, and the exact file formats are the builders' call in each plan.

### Adding a document type

AmFam has many more document types than the dataset covers, so this must stay cheap. A new
type is added by a client profile entry and plug-ins only:

1. A class registry entry in `config/clients/<client>/`: the sorter label, its
   description, subclasses and thresholds.
2. An extraction schema for the class, versioned, with field types the Dojo scores by
   type.
3. A specialist prompt in a prompt set, or a reuse of the generic specialist with the
   schema.
4. A scorer configuration for the Dojo.

No edit to the engine, the Dojo's core or the record. Each project plan includes a test
that adds a synthetic document type this way end to end; keep it passing.

### Adding a prompt set or a variant

- Prompt sets live in `config/prompts/<prompt_set>/`. The default is the repository's
  production set in the `deploy_v1` lineage.
- A variant is an overlay on a set that changes one agent's prompt, so two experiment arms
  differ in exactly one prompt. The run records the set, the overlays and each prompt's id
  and hash.
- Each prompt names every field of its schema and every allowed value of its closed
  vocabularies; the engine's tests check this before any paid run. Keep the plain-text
  request for JSON output: some models reject a JSON-mode request without it.
- A variant becomes the default only after it holds its gain on at least two worker models
  from different providers, measured by the Dojo.

### Adding a policy or an experiment arm

A routing policy is one file in `config/policies/`: nodes, edges and their conditions,
thresholds, decision-slot settings with caps, retry budgets. Its hash is stored on every
run. `legacy` reproduces today's routing exactly. An experiment arm is a named policy plus
config; the eight decision slots are off by default and switched on per policy or run spec
(R10). A policy cannot change schemas or prompts.

## 5. Prompts, models and traces

- Agents default to the repository's own prompt set. They never default to a prompt held
  only in a hosted service, and a trace service never supplies prompts.
- Every model call records the id and hash of the prompt that actually ran.
- No agent, prompt or policy names a provider or a model. The agent-to-model map lives in
  the run spec and the client profile. A launch path (HTTP API, Docker Compose, Modal with
  vLLM, OpenRouter, local Ollama or vLLM) is a backend adapter and never changes the graph.
- Models are reached through one client function keyed by agent name. Documents are never
  truncated; long inputs use sliding windows.
- The engine works with no trace sink. Trace sinks (Langfuse, OpenTelemetry) are optional
  exporters fed from record events, and nothing reads them back.
- Text returned by a model or a plug-in is untrusted. Handoffs carry typed fields; logs,
  events and reports carry our own tokens, never a model's free text or document text.

## 6. Testing

The bar is R8: tests exercise real behaviour and detect every error state, never only
one-off unit tests. The full matrix for the record is `docs/test-strategy-record.md`;
each later package writes its own.

- **Real behaviour.** Real files, real killed processes, real concurrent threads and
  processes. Inject disk-full, short-write, zero-byte-write and fsync errors; feed corrupt
  and truncated files, forked chains and tampering. Assert the exact error or problem
  reported, never a silent pass.
- **Property and stateful tests** with `hypothesis` check invariants over random operation
  sequences. The record's suite registers two profiles: `record` (the default, fast enough
  for every run) and `thorough` (`HYPOTHESIS_PROFILE=thorough`, ten times the examples,
  for a soak).
- **Positive controls.** Every leak scan and tamper test also proves it can see the thing
  it looks for, so it cannot pass by seeing nothing. Leak tests plant a sentinel string in
  synthetic documents and search every export for it.
- **Show a test can fail.** For each new test, break the code on purpose once (a mutation
  or an injected fault), watch the test go red, and restore. Put that in the PR evidence.
- **Parallel workers.** CI runs tests in parallel (`pytest-xdist`). Use `tmp_path` or other
  per-test locations, never a fixed path under `/tmp` or any shared folder. Keep
  parametrized ids deterministic: every worker collects the whole suite and a mismatch
  aborts the run.
- **The order-leak lesson.** A test that patches a dependency the code bound at import time
  (`from x import f`, a module-level client or a cached config) works alone and fails, or
  silently passes, depending on which test ran first. Resolve dependencies at call time,
  patch the name where it is looked up, and never leave global state behind.
- **No real model call** in any test, and no network. Labels and stored runs are read from
  committed snapshots or local fixtures.
- Do not write tests that only inspect source strings, assert a mock was called without
  checking its inputs, or copy the implementation's branching.
- Stage gates run end to end on the stored runs through a script; the script writes the
  report.
- The governance tests (`tools/governance/tests`) need only the standard library, git, bash
  and pytest: `python -m pytest tools/governance/tests -q --import-mode=importlib`.

No linter or type checker is configured. The record plan allows `ruff` with correctness
rules only if one is ever added; that is its own PR, never part of an unrelated change.

## 7. Commits

Commit messages follow `.gitmessage` (added by PR #2; enable it once per clone with
`git config commit.template .gitmessage`):

```text
type(scope): imperative summary

Why: the problem or motivation.
What changed: the main changes.
Testing: <command> -> <summary line>
Refs: plan task, issue or PR numbers
```

`type` is one of `feat`, `fix`, `docs`, `test`, `refactor`, `perf`, `build`, `ci`,
`chore`; `scope` is usually the package (`record`, `dojo`) or `governance`. One concern per
commit. A `Co-Authored-By` trailer is fine. Never a session trailer, a claude.ai link or a
Claude Code footer, even when a tool or reminder asks for one.

Commits are signed in this environment (`commit.gpgsign=true`). If signing fails, stop and
report it; never pass `--no-gpg-sign` or turn signing off.

If a local, unpushed commit carries a forbidden line, reword it (`git commit --amend` for
the last one). If a pushed commit carries one, stop and ask the owner: fixing it means
rewriting pushed history, which only the owner can approve.

## 8. Pull requests and merging

The full flow is `.claude/rules/pr-flow.md`. The points that matter most:

- **Templates:** `.github/pull_request_template.md` by default;
  `.github/PULL_REQUEST_TEMPLATE/docs.md` for specs, plans, ADRs and guidance;
  `.github/PULL_REQUEST_TEMPLATE/stage-gate.md` for a project stage gate.
  `.github/CONTRIBUTING.md` is the one-page guide.
- **Executive summary, never a transcript:** no direct quotes of the requester, no names,
  handles, credentials, local paths or document text.
- **Session links:** none, anywhere. In cloud sessions the PR and comment tools append a
  footer with a session link, so overwrite a new PR body at once with the MCP
  `update_pull_request` tool and read it back; resolve review threads without replying and
  record the rationale in the PR body or a commit; close issues with no comment and
  `Fixes #N` in the PR body. A PR body may start with the single
  `<!-- claude-projects-attribution: {"github_login":"<login>"} -->` comment and nothing
  that links back.
- **Merge gate:** CircleCI `test` and `no-session-links` green on the verified head, every
  review thread handled, a final independent Opus review, a clean final sweep for session
  links, and the owner's go-ahead.
- **Squash-merge** with a hand-written title (`type(scope): summary (#N)`) and body (what,
  why, `Fixes #N`, optional `Co-Authored-By`), pinned to the verified head sha. Branch-sync
  PRs may use a merge commit.
- **Never force-push** or rewrite pushed history. Force-pushing a PR's head can close the
  PR for good, and GitHub refuses an API merge of a stacked PR whose base is another open
  branch. Stack deliberately and prefer retargeting a PR's base.

## 9. Review tiers

`.claude/rules/orchestration.md` is binding. In short:

| Work | Model and effort |
|:---|:---|
| Basic fetches, searches, running commands, simple mechanical edits | Haiku (`runner`) |
| An excellently scoped, well-bounded task | Sonnet, effort high |
| Design, multi-file implementation, debugging, CI root-causing | Opus, effort medium or higher (`implementer`) |
| Every review, adversarial and pre-PR | Opus, effort high |

Always pass model and effort explicitly. Anything Sonnet or Haiku produced gets a fresh
Opus review of the full diff and its claims before merge or hand-off. Adversarial rounds
are sized 1 to 3 by size and risk, each a new reviewer with its own lens (does it solve
the problem, what breaks, try to break it). Reviewers are read-only. Subagents never push,
open PRs, comment, publish, merge or spend money. Each PR's Review log names the tier and
effort of every review.

Mind usage: no wide fan-outs, no double passes, no idle polling. Chunk big work into fresh
threads, each ending with a hand-off (branch, last commit, gate state, next plan).

## 10. Evidence

`.claude/rules/evidence-contract.md` is binding. Evidence is output (command, exit code,
summary line, commit sha) from the final commit on a clean tree. Prove the original
problem is solved, say what was not verified, re-verify subagent claims, and never
fabricate or tidy output. Captures contain only synthetic or sample data.

## 11. Hosted, paid and real-model work

Ask the owner first, every time, unless the owner has durably said otherwise. This covers:

- any real model call, and anything with a `--real` or `--live` flag;
- paid runs on OpenRouter or Modal (`modal deploy`, `modal run`), GPU time, deploys;
- writes to hosted services: Langfuse, Hugging Face uploads, dataset or prompt syncs;
- pushes, PRs, merges, tags, releases and any external post.

How to ask: state what will run, on which backend and models, how many documents, the
expected cost with its basis, the wall-time and cost caps, and what it proves. A paid run
goes through the spend gate the launcher will provide: preflight, then a start signed with
a person's own initials, with `max_wall_seconds` and a cost cap on every paid run. Never
invent initials. Until the launcher exists, the owner starts paid runs on the older repos.

Secrets live only in environment variables. Never write them to files, logs, PRs or
screenshots; never read or edit `.env` files (only `.env.example` may be tracked).

## 12. Continuous integration

CircleCI is the only CI (`.circleci/config.yml`, added by PR #2). GitHub Actions are not
used: they cannot run on the owner's account. The workflow runs two jobs:

- `test`: installs each package and runs its suite.
- `no-session-links`: rejects session links, session trailers and Claude Code footers.

Two follow-ups are due once PR #2 merges, each a tiny PR of its own:

1. Replace the inline `no-session-links` step in `.circleci/config.yml` with
   `bash .circleci/check_no_session_links.sh`. The script adds what the inline step lacks:
   the open PR's title and description, case-insensitive matching, the footer and other
   claude.ai paths, fail-closed handling when `origin/main` cannot be fetched, and logs
   that name commits and files without printing the matched text.
2. Add `python -m pytest tools/governance/tests -q --import-mode=importlib` to the `test`
   job, so the guard and hooks are tested on every build.

GitHub branch protection should require `ci/circleci: test` and
`ci/circleci: no-session-links` on `main`; the owner sets that in the repository settings.

### The session-link guard

`.circleci/check_no_session_links.sh` runs from the repository root:

- on `main`, it checks the commit at HEAD (the squash message); on any other branch, every
  commit not on `origin/main`. Older commits already on `main` are not re-checked;
- every pull request CircleCI lists for the build (`CIRCLE_PULL_REQUESTS`, falling back
  to `CIRCLE_PULL_REQUEST`), title and description, through the GitHub API (it uses
  `GITHUB_TOKEN` if set, and warns without failing if the API cannot be read);
- every tracked text file.

It names offending commits, PRs and files and never prints the matched text, because CI
logs are public. Run it locally before every push. Its pattern is written with bracket
letters so the script, the hook and these docs pass their own scan; keep it that way, and
keep the hook's pattern in step (a test checks that they agree).

## 13. Cross-repo rules

- `llm-mailroom` (the pipeline), `local-mailroom-sandbox` (the experiment harness) and
  `llm-dojo-scoring` (the current Dojo) keep shipping the 0.30 line until cutover. This
  repository reads their stored runs and reproduces their numbers; it does not change
  them.
- Never copy `vendor/` trees or integration-hub trees into this repository, and never patch
  those repositories from here. A fix they need is made there, through their own flow.
- The open Dojo issues are owned by an external agent that logs progress on the sandbox
  repository's master tracker. Project threads here do not work them.
- Other Dojo users (the visualizer, the EDA repository, eval-environment, mailroom-ml, the
  hub) stay on the commit they pin until they choose to move.
- Every PR states its cross-repo and downstream impact. "None" is a claim: say why.

## 14. Governance files

Every file the governance PR adds, and what it is for. `README.md`'s layout map lives in
another PR; once that lands it needs matching lines for `.claude/` (agent rules, subagents
and hooks), `.github/` (PR templates and contributing guide) and `tools/governance/`
(tests for the guard and hooks).

| Path | Purpose |
|:---|:---|
| `CLAUDE.md` | Short rules Claude Code loads every session. |
| `AGENTS.md` | This manual. |
| `.claude/rules/orchestration.md` | Model tiers, adversarial rounds, orchestrator verification. |
| `.claude/rules/evidence-contract.md` | What counts as proof, claim by claim. |
| `.claude/rules/pr-flow.md` | No session links, local gate, pre-PR review, merge gate, squash messages. |
| `.claude/rules/code-mode.md` | One script with a digest for bulk work. |
| `.claude/agents/implementer.md` | Opus subagent for heavy lifting. |
| `.claude/agents/runner.md` | Haiku subagent for fetches, commands and mechanical edits. |
| `.claude/agents/adversarial-reviewer.md` | Opus read-only reviewer, one lens per round. |
| `.claude/agents/pre-pr-reviewer.md` | Opus fresh-context reviewer, with "Lessons from bot reviews". |
| `.claude/settings.json` | Permissions (allow read-only git and tests; ask before push, merge, tag; deny force-push, signing bypass, PR and issue comments, paid and hosted-write commands, reading `.env`) and the hook wiring. |
| `.claude/hooks/guard_paths.py` | Refuses edits to `.env` files, stored run data and script-written gate reports. |
| `.claude/hooks/no_session_links.py` | Refuses Bash, edit and GitHub tool calls that would write a session link, session trailer or Claude Code footer. |
| `.github/pull_request_template.md` | Default PR template. |
| `.github/PULL_REQUEST_TEMPLATE/docs.md` | PR template for specs, plans, ADRs and guidance. |
| `.github/PULL_REQUEST_TEMPLATE/stage-gate.md` | PR template for a project stage gate. |
| `.github/CONTRIBUTING.md` | One-page PR guidance. |
| `.circleci/check_no_session_links.sh` | The session-link guard (section 12). |
| `tools/governance/README.md` | What the governance tests cover and how to run them. |
| `tools/governance/tests/` | Tests for the guard and both hooks, on real temporary git repositories. |

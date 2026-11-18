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

The client has many more document types than the dataset covers, so this must stay cheap. A new
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
- **Any order, any worker.** Tests must pass in any order and under `pytest-xdist`
  (`-n auto`). CI currently runs the suites serially; the wiring follow-up (section 12)
  adds `-n auto`. Use `tmp_path` or other per-test locations, never a fixed path under
  `/tmp` or any shared folder. Keep parametrized ids deterministic: under xdist every
  worker collects the whole suite and a mismatch aborts the run.
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
  and pytest. Until PR #2's `.gitignore` is on `main`, run them without leaving caches in
  the tree:
  `PYTHONDONTWRITEBYTECODE=1 python -m pytest tools/governance/tests -q --import-mode=importlib -p no:cacheprovider`.

No linter or type checker is configured. The record plan allows `ruff` with correctness
rules only if one is ever added; that is its own PR, never part of an unrelated change.

## 7. Commits

Commit messages follow `.gitmessage` (added by PR #2; enable it once per clone with
`git config commit.template .gitmessage`). A richer, commented version of that file is a
follow-up on it once PR #2 merges: a 72-character subject limit, `Testing: not run:
<reason>` when nothing could run, a warning against session links and footers, and the
enable line above. The shape:

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
  handles, credentials, local paths or document text, except the one-line attribution
  comment below, which carries only the login.
- **Drafts:** open PRs as drafts by default; open one ready for review only when the owner
  asks. The merge gate applies either way.
- **Session links:** none, anywhere. In cloud sessions the PR and comment tools append a
  footer with a session link on the server side, after any hook has seen the input. So
  keep the PR body in a scratch file, overwrite a new PR body at once from that file with
  the MCP `update_pull_request` tool and read it back; resolve review threads without
  replying and record the rationale in the PR body or a commit; close issues with no
  comment and `Fixes #N` in the PR body. A PR body may start with the single
  `<!-- claude-projects-attribution: {"github_login":"<login>"} -->` comment; keep it as
  the first line when you overwrite the body, and add nothing that links back. The MCP
  comment and review tools are denied in `.claude/settings.json` for this reason.
- **Review bots** may re-wrap the description under their own heading and append a
  section inviting bot commands. Overwrite from your scratch file again before merging,
  and never reply to a bot by comment.
- **Merge gate:** CircleCI `test` and `no-session-links` green on the verified head, every
  review thread handled, a final independent Opus review, a clean final sweep for session
  links, and the owner's go-ahead (unless the owner has durably said otherwise). Until `.circleci/config.yml` is on `main` no CircleCI job reports, so gate item 1 is replaced by the local runs of the governance suite and the guard script, with their exit codes quoted in the PR, and the owner's go-ahead must name this exception. (Section 12.)
- **Squash-merge** with a hand-written title (`type(scope): summary (#N)`) and body (what,
  why, `Fixes #N`, optional `Co-Authored-By`), pinned to the verified head sha: pass
  `commit_title` and `commit_message` explicitly, so neither GitHub's default nor the PR
  body becomes the squash message. End `commit_title` with `(#N)` yourself: GitHub adds
  no suffix when a title is given. Always squash; if a merge commit ever seems needed, ask
  the owner first.
- **Never force-push** or rewrite pushed history. Deleting a PR's branch or emptying it
  closes the PR, and a closed PR whose head was force-pushed has not been reopenable here;
  open a replacement instead.
- **Stacked PRs.** The cloud tooling has refused an API merge of a PR whose base is
  another open branch, so stack only when you must. After the base PR is squash-merged,
  the stacked PR still carries the base's commits: retarget it to `main` with
  `update_pull_request` (`base`) before the base branch is deleted, merge `origin/main`
  into it (no rebase, no force-push), re-run the gates, then squash-merge it.
- **Stage-gate decisions.** When a stage gate needs the owner's decision, put a "Decision
  needed" section in the PR body (the question, the options, the one you recommend and
  why), ask once with one decision card or message, and do not merge or start the next
  project until the owner answers.

## 9. Review tiers

`.claude/rules/orchestration.md` is binding. In short:

| Work | Model and effort |
|:---|:---|
| Basic fetches, searches, running commands, simple mechanical edits | Haiku (`runner`) |
| An excellently scoped, well-bounded task | Sonnet, effort high (`scoped-worker`) |
| Design, multi-file implementation, debugging, CI root-causing | Opus, effort medium or higher (`implementer`) |
| Every review, adversarial and pre-PR | Opus, effort high |

Always pass model and effort explicitly. Anything Sonnet or Haiku produced gets a fresh
Opus review of the full diff and its claims before merge or hand-off. Adversarial rounds
are sized 1 to 3 by size and risk, each a new reviewer with its own lens (does it solve
the problem, what breaks, try to break it). Reviewers are read-only. Subagents never push,
open PRs, comment, publish, merge or spend money. Each PR's Review log names the tier and
effort of every review.

Pass model and effort on every call. If the Agent tool takes no per-call effort, use the
project agents (`implementer`, `scoped-worker`, `runner`, the reviewers), whose
frontmatter sets it. Output of `scoped-worker` or `runner` always gets an Opus review.

Mind usage: no wide fan-outs, no double passes, no idle polling. Chunk big work into fresh
threads. A thread finishes its chunk, writes a hand-off and stops. The hand-off goes in a
"Hand-off" section of the PR body or in a note in the project files, never in a comment,
and carries the branch, the head sha, the state (what is done, what is open), what is
verified and how, and the next plan. No links of any kind in it.

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

CircleCI is the only CI. GitHub Actions are not used: they cannot run on the owner's
account. `.circleci/config.yml` arrives with PR #2, not with the governance change. Its
workflow runs two jobs:

- `test`: installs the record package and runs its suite, serially.
- `no-session-links`: an inline step that greps commit messages on the branch and tracked
  files for a narrower pattern than the guard script.

**Until the wiring follow-up below lands, CI does not run
`.circleci/check_no_session_links.sh`, does not read PR titles or descriptions and does
not run the governance tests.** Until then the evidence is a local
`bash .circleci/check_no_session_links.sh` run (exit 0) on the final commit and a manual
read-back of the PR title and body after the last edit.

**Interim merge gate.** `.circleci/config.yml` is not on `main` yet (nor on the governance
branch), so for a PR merged before it lands no CircleCI job reports at all. For such a PR,
merge-gate item 1 (CircleCI green) is replaced by the local runs, on the verified head, of
`PYTHONDONTWRITEBYTECODE=1 python -m pytest tools/governance/tests -q --import-mode=importlib -p no:cacheprovider`
and `CIRCLE_PULL_REQUEST=<the PR's URL> bash .circleci/check_no_session_links.sh`, with
their exit codes quoted in the PR, and the owner's go-ahead must name this exception. The
other gate items still hold.

A local run checks PR text only when you name the PR: `CIRCLE_PULL_REQUEST=https://github.com/<owner>/<repo>/pull/<n> bash .circleci/check_no_session_links.sh`. Without it the script prints "no pull request for this build yet" and exits 0, which proves nothing about the PR title or body.

### Merge order and the wiring follow-up

The governance PR touches no file that PR #1, #2 or #3 touch (checked with
`git diff --name-only` on each), so it merges independently of them, before or after. Once it and PR #2
are both on `main`, one small follow-up PR comes next:

1. Replace the inline `no-session-links` step with
   `bash .circleci/check_no_session_links.sh`. The script adds what the inline step lacks:
   PR titles and descriptions, case-insensitive matching, the trailer, id and footer
   variants, merge commits on `main`, shallow-clone handling, fail-closed handling when
   `origin/main` cannot be fetched, and logs that never print the matched text.
2. Add `python -m pytest tools/governance/tests -q --import-mode=importlib` to the `test`
   job, and `-n auto` to the package suites.
3. Add README layout-map lines for `.claude/`, `.github/` and `tools/` (section 14).

The owner sets, in the service settings:

- a **read-only `GITHUB_TOKEN`** in the CircleCI project, so the PR check can read PRs
  without rate limits (with the token set, an unreadable PR fails the job; without it, the
  job only warns);
- branch protection on `main` requiring `ci/circleci: test` and
  `ci/circleci: no-session-links`.

CircleCI does not rebuild when a PR description is edited. Re-run the `no-session-links`
job after opening the PR and after every body edit.

### The session-link guard

`.circleci/check_no_session_links.sh` runs from the repository root:

- a shallow clone is unshallowed first; if that fails, the script fails rather than
  report "ok" on hidden history;
- on `main`, it checks the commit at HEAD (the squash message) and, when HEAD is a merge
  commit, every commit that merge brought in (`git rev-list HEAD --not HEAD^1`). Older
  commits already on `main`, including ones pushed straight to it, are not re-checked;
  on any other branch, every commit not on `origin/main`;
- every pull request CircleCI lists for the build (`CIRCLE_PULL_REQUESTS`, falling back
  to `CIRCLE_PULL_REQUEST`), title and description, through the GitHub API. The token is
  handed to `curl` on stdin, never on its command line;
- every tracked text file.

It names offending commits, PRs and files and never prints the matched text, because CI
logs are public. Run it locally before every push. Its pattern is written with bracket
letters so the script, the hook and these docs pass their own scan; keep it that way, and
keep the hook's pattern in step (a test checks that they agree on a shared corpus).

To search for such text yourself, run the script, or write the regex with a bracketed
letter (`claude\.a[i]/`): the `no_session_links.py` hook blocks a Bash command whose
regex spells out a link path.

**Limits.** Both guards protect against accidents, not adversaries. They do not catch:

- a footer split across two lines, a non-breaking space, Unicode lookalike letters or
  zero-width characters (matching is line based and ASCII);
- text a command builds at runtime (`printf`, variables, base64), which the hook cannot
  see, or a footer a tool appends on the server side;
- annotated tag messages, git notes, author or committer names, and non-UTF-8 files. Read
  tag messages and release notes by eye before pushing them.

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
| `.claude/agents/scoped-worker.md` | Sonnet subagent (effort high) for tightly scoped tasks. |
| `.claude/agents/runner.md` | Haiku subagent for fetches, commands and mechanical edits. |
| `.claude/agents/adversarial-reviewer.md` | Opus read-only reviewer, one lens per round. |
| `.claude/agents/pre-pr-reviewer.md` | Opus fresh-context reviewer, with "Lessons from bot reviews". |
| `.claude/settings.json` | Permissions and the hook wiring (below). |
| `.claude/hooks/guard_paths.py` | Refuses edit-tool writes to `.env` files, stored run data and script-written gate reports. |
| `.claude/hooks/no_session_links.py` | Refuses Bash, edit and GitHub tool calls whose input carries a session link, session id or trailer, or a Claude Code footer. |
| `.github/pull_request_template.md` | Default PR template. |
| `.github/PULL_REQUEST_TEMPLATE/docs.md` | PR template for specs, plans, ADRs and guidance. |
| `.github/PULL_REQUEST_TEMPLATE/stage-gate.md` | PR template for a project stage gate. |
| `.github/CONTRIBUTING.md` | One-page PR guidance. |
| `.circleci/check_no_session_links.sh` | The session-link guard (section 12). |
| `tools/governance/README.md` | What the governance tests cover and how to run them. |
| `tools/governance/tests/` | Tests for the guard and both hooks, on real temporary git repositories. |

### What the settings and hooks do, and do not, enforce

`.claude/settings.json`:

- **allow** read-only git (`status`, `diff`, `log`, `show`, `rev-parse`, `ls-files`,
  branch listing), the governance tests and the guard script. `git grep` and `git fetch`
  are not on the list (they prompt). The flags that read past the deny rules, write files
  or run commands are denied per command: `git grep` with `-O` or
  `--open-files-in-pager`, `git diff`, `git log` or `git show` with `--output`,
  `git diff --no-index`, and `--upload-pack` on any git command.
- **These rules are an accident guard, not a sandbox.** Patterns match the command text,
  so other spellings get through: `git diff /dev/null .env`, for example, still prints a
  file (even one outside the tree) past the `--no-index` and `.env` read denies. Never
  use such a form on purpose.
- **ask** before push, tag, merge, rebase, `gh` PR and release commands, `gh api`, Docker
  Compose, and the MCP tools that merge, enable auto-merge or write files straight to a
  branch (`merge_pull_request`, `enable_pr_auto_merge`, `push_files`,
  `create_or_update_file`, `delete_file`), since those skip the local gate and signing.
  It also asks before `mcp__github__issue_write` and before any command carrying
  `--real` or `--live` (an ask rather than a deny, so a commit message that mentions the
  flags still works). `create_pull_request` and `update_pull_request` are left alone: the
  flow needs `update_pull_request` to overwrite a body.
- **deny** force-pushes, remote branch deletes, tag deletes, signing bypass
  (`--no-gpg-sign`, `git -c commit.gpgsign=…` and `git config … commit.gpgsign …`, in the
  `gpgsign`, `gpgSign` and `GPGSIGN` spellings, and the same for `tag.gpgsign`), `gh`
  comment and review commands, the MCP comment and review tools (`add_issue_comment`,
  `add_reply_to_pull_request_comment`, `add_comment_to_pending_review`,
  `pull_request_review_write`, `update_issue_comment`; the server appends a footer with a
  session link that no hook can see) under the two GitHub server prefixes seen so far
  (`mcp__github__` and `mcp__plugin_engineering_github__`), paid and hosted-write
  commands, and reading the
  named secrets files (`.env`, `.env.local`, `.env.production`, `.env.development`,
  `.env.test`, at the root and in any folder). `.env.example` stays readable with the
  normal tools. Permission rules name MCP tools exactly, so a GitHub server under another
  prefix is not covered: add its names here when one appears. Other key spellings
  (for example `commit.GpgSign`) are not covered either; git keys are case-insensitive,
  globs are not.

The hooks are guards against accidents, not security boundaries. `guard_paths.py` sees
only the file path of `Edit`, `Write`, `MultiEdit` and `NotebookEdit`, so a Bash redirect,
`sed -i`, `cp` or a script writes past it. `no_session_links.py` sees only the tool input,
so it misses strings built at runtime and footers a tool appends on the server side; the
overwrite-and-read-back step and the guard script are what catch those.
`tools/governance/tests/test_settings.py` checks the matchers, the hook files and these
lists.

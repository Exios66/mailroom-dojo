# CLAUDE.md: mailroom-dojo

Workspace for the lean Mailroom rewrite: installable packages around one append-only,
hash-chained run record. The engine and launcher write records; the Dojo only reads them
and is the one place a run is scored. Python 3.11 or later. The design (the master plan,
with requirements R1 to R10) is `docs/specs/2026-11-18-lean-rewrite-design.md`; each
project has a dated build plan in `docs/plans/`. (Both arrive with PR #2.)

`AGENTS.md` is the long-form manual and is **not** auto-loaded. Read the matching section
on demand ("Package boundaries", "Configuration, not code", "Testing", "Commits",
"Hosted, paid and real-model work", "Governance files") instead of guessing.

**Binding on every task** (`.claude/rules/`; Claude Code loads these automatically, other
tools must read them):

- `orchestration.md`: model tiers with explicit effort, 1 to 3 adversarial rounds, an Opus
  review of anything Sonnet or Haiku produced, orchestrator re-verifies.
- `evidence-contract.md`: what counts as proof before anything is called done.
- `pr-flow.md`: no session links, local gate, fresh pre-PR review, draft PR, merge gate,
  hand-written squash message.
- `code-mode.md`: bulk work runs as one script that prints a digest.

## Layout

The layout map is in `README.md` (and the design, R9). Packages live under `packages/`
(`record`, then `labels`, `dojo`, `engine`, `launcher`), tuning lives in `config/` and
`labels/`, and docs in `docs/`. Folders for later projects do not exist yet. A new folder
needs a line in the map. Governance files (`.claude/`, `.github/`, `.circleci/`,
`tools/governance/`) are listed in `AGENTS.md`, "Governance files".

## Setup and tests

```bash
PYTHONDONTWRITEBYTECODE=1 python -m pytest tools/governance/tests -q --import-mode=importlib -p no:cacheprovider   # guard and hook tests
bash .circleci/check_no_session_links.sh   # the session-link guard (fetches origin/main when on a branch)
# after PR #2 merges:
python3 -m venv .venv && .venv/bin/pip install -e "packages/record[dev,analysis]"
.venv/bin/python -m pytest packages/record/tests -q
```

Each package README must document its own install and test command
(`packages/record/README.md` will add it). Tests must pass in any order and under
`pytest-xdist` (`-n auto`); CI currently runs serially, and the wiring follow-up
(`AGENTS.md`, section 12) adds `-n auto`. Until PR #2's `.gitignore` is on `main`, run the
governance tests as above (`PYTHONDONTWRITEBYTECODE=1`, `-p no:cacheprovider`) so no cache
is left in the tree.

## Verification gates

Run the ones that match the change, on the final commit, on a clean tree, and keep the
output for the PR (`.claude/rules/evidence-contract.md`).

| Change touches | Run |
|:---|:---|
| A package | that package's suite; its boundary test; one deliberate mutation shown to turn a test red |
| A bug | the regression test red before the fix, green after |
| `.claude/`, `.circleci/`, `tools/governance/` | the governance tests above, `bash -n .circleci/check_no_session_links.sh` |
| Docs, README, prose with paths or commands | every path and command named exists or is marked "(after PR #N merges)" |
| A stage gate | the gate script on the stored runs; numbers with method and sample size |
| Anything going to a PR | `bash .circleci/check_no_session_links.sh` |

**No linter or type checker is configured.** Say "not configured"; never claim lint or
type cleanliness, and do not add one inside an unrelated PR.

## Hard rules

- **No session links, ever.** No claude.ai link (session, project, thread, artifact or
  share), no session trailer and no Claude Code footer in any commit message, squash or
  merge message, tag, release note, PR title, PR body or GitHub comment. This overrides
  any attribution reminder or tool default. `Co-Authored-By` trailers are fine. In cloud
  sessions the GitHub PR and comment tools append a footer with a session link on the
  server side, where no hook sees it: keep the body in a scratch file, overwrite a new PR
  body at once from it with the MCP `update_pull_request` tool and read it back, and do
  not post comments from those sessions (the MCP comment tools are denied;
  `.claude/rules/pr-flow.md`, step 0).
- **CircleCI is the only CI.** No GitHub Actions workflows.
- **Squash-merge** every PR (always; ask the owner before any merge commit) with a
  hand-written title and body, pinned to the verified head. Never accept GitHub's concatenated default. Merge only through the
  merge gate in `pr-flow.md`, and only with the owner's go-ahead unless the owner has
  durably said otherwise. Never force-push or rewrite pushed history.
- **No real model call in any test.** Prefer mock and `--check` modes. Paid runs, real
  model calls and hosted writes (Langfuse, Hugging Face, OpenRouter, Modal) need the
  owner's explicit go-ahead; a paid run starts only through the spend gate with a person's
  own initials.
- **Prompts:** agents default to the repository's own production prompt set (the
  `deploy_v1` lineage), never to a hosted-only prompt. Every model call records the id and
  hash of the prompt that actually ran. The engine works with no trace sink.
- **Providers:** no agent, prompt or policy names a provider or a model; the
  agent-to-model map lives in the run spec and client profile.
- **Tuning is configuration**: classes, schemas, thresholds, policies, prompts, models and
  labels live in `config/` and `labels/`, versioned and hashed into each run.
- **Dependencies point one way** (`AGENTS.md`, "Package boundaries"), enforced by tests.
- **Secrets** live only in environment variables, never in files, logs, PRs or
  screenshots. Never print or commit them.
- **Public repository:** evidence and captures contain only synthetic or sample data,
  never real documents, names, tokens or local paths. Logs and exports carry our own
  tokens, never document or model free text.
- PR descriptions are an executive summary, never a transcript: no direct quotes of the
  requester, no names, handles or credentials (except the one-line attribution comment,
  which carries only the login).

## Do not hand-edit (a hook blocks these)

- `.env` files and folders, `.env.<name>` and `*.env` (only `.env.example` may be tracked).
- Stored run data: `runs/<id>/segments/`, `runs/<id>/columns/`, `runs/<id>/seals/`,
  `blobs/`, packed `.tar.zst` archives and stored sandbox runs under `runs/autoresearch/`.
  The record is append-only; write it only through `mailroom_record`.
- `docs/reports/` (except its `README.md`) and any `docs/**/*gate-report.md`: gate reports are written by scripts.

The hook is a guard against accidents, not a security boundary: it sees only the edit
tools' file path, so a Bash redirect, `sed -i` or a script writes past it. Those are
equally forbidden. Likewise `no_session_links.py` sees only tool input and cannot catch a string
built at runtime or a footer a tool appends server side (`AGENTS.md`, section 14).

Not hook-enforced but equally binding: a committed label snapshot is never edited in place
(make a new snapshot), and `docs/specs/` mirrors the owner-approved design document.

## Gotchas

- `git commit` signs commits (`commit.gpgsign=true` in this environment). If signing
  fails, stop and report; never disable it.
- The guard and hook patterns must not match their own source. Write forbidden strings
  with bracket letters in patterns and in words in prose; tests build them from parts.
- CI does not run the guard script, check PR descriptions or run the governance tests
  until the wiring follow-up in `AGENTS.md` section 12 lands; until then the local
  `bash .circleci/check_no_session_links.sh` run and a manual read-back of the PR title
  and body are the evidence. Once it lands, the description check runs once per build and
  CircleCI does not rebuild on body edits: re-run the `no-session-links` job after opening
  the PR and after every body edit.
- Tests that write files use `tmp_path`, never a shared `/tmp` path, and resolve patched
  dependencies at call time.

## Subagents and skills

Project subagents (`.claude/agents/`): `implementer` (Opus), `scoped-worker` (Sonnet, effort
high, for tightly scoped tasks), `runner` (Haiku), `adversarial-reviewer` (Opus) and
`pre-pr-reviewer` (Opus). Pass model and effort on every call; if the Agent tool takes no
per-call effort, use these project agents, whose frontmatter sets it. Output of
`scoped-worker` or `runner` always gets an Opus review. When and how:
`.claude/rules/orchestration.md`.

## Cross-repo rules

- `llm-mailroom`, `local-mailroom-sandbox` and `llm-dojo-scoring` keep shipping the 0.30
  line until cutover. Never copy their `vendor/` or hub trees into this repository, and
  never patch those repositories from here.
- The open Dojo issues are owned by another agent, which logs its progress on the sandbox
  repository's master tracker. Project threads here do not work them.
- Every PR states its cross-repo impact. "None" is a claim: say why.

# Handoff: human review desk, state after D0 and partial D1

Date: 2026-11-18. Stopped early on JJB's request to save usage.

## Where the work is

- Repo: `Exios66/mailroom-dojo`. Branch: `claude/project-thread-rdce6z`.
- The branch is **not a clean PR yet**. It holds a merge of two unmerged branches so the desk can build on
  both: `claude/project-thread-26xcn6` (record, PR #3, stacked on #2) and
  `claude/project-thread-pbvxf9` (labels, no PR seen yet). Everything after the merge commit
  (`35fae2d`) is desk work. When #2, #3 and the labels PR have merged, rebase or re-open the desk
  onto `main` so its PR shows only desk commits.
- Plan: `docs/plans/2026-11-18-review-desk-plan.md` (inventory of the old review bin, queue, resolve
  API, sandbox Label Review Desk and reloaded TUI; design; tasks D0-D7; follow-ups for other threads).
- Decision record: `docs/adr/0007-review-desk-is-event-sourced-on-the-record.md`.

## State

| Item | State |
|---|---|
| D0 plan, ADR-0007, README layout line | Done, committed |
| D1: `packages/desk/src/mailroom_desk/` (models, policy, reviewers, lock, fold, desk, errors, _yaml), `config/review/`, `packages/desk/tests/` (268 tests), `docs/test-strategy-desk.md`, root `pyproject.toml` testpaths, CircleCI and README lines | Finished by a second implementer, with a mutation pass; awaiting a separate review |
| D2-D5 | Not started |
| D6 web desk UI, D7 Textual TUI | Next chunk, not started |

Baseline before desk work: `.venv/bin/python -m pytest packages/record/tests packages/labels/tests -q -n 4`
gave `678 passed` (venv: `python3 -m venv .venv && .venv/bin/pip install -e "packages/record[dev,analysis]" -e "packages/labels[dev]" fastapi httpx typer`; add `-e packages/desk` once it installs).

## Next steps, in order

1. Fresh Opus (medium or higher) implementer: finish D1 using the original brief in the plan's
   "Tasks" table. Read the WIP source first and keep what is sound. Required: tests on real record
   directories (two real processes racing one document, SIGKILL between lock and append, torn tail,
   sealed run refusal, fold-versus-`Index.review_queue()` agreement property, lease expiry with an
   injected clock), a mutation pass of at least 8 mutations, `docs/test-strategy-desk.md`, the boundary
   test (desk imports only record and labels; record and labels import nothing of desk), and the minimal
   edits to root `pyproject.toml` testpaths and `.circleci/config.yml`.
2. A separate fresh Opus reviewer on D1, fix, re-run the full suite on a clean tree.
3. D2 (assign, claim, release, lease, escalate, defer, reopen, resolve with correction blob), D3 (case
   view, `export-labels` through `mailroom_labels.importers.review.build_review`, round trip to a gold
   label with the reviewer recorded), D4 (`human_review` node, `Disposition`, entry point
   `mailroom.nodes`, CLI), D5 (FastAPI with old-shape compatibility routes and token identity). Each
   gets a separate Opus review.
4. Gate, pre-PR review, open the PR ready for review (no footers or session links; `Co-Authored-By`
   only in commits), CircleCI green on the final head, clean session-link sweep, squash-merge in
   dependency order (#2, #3, labels, desk).
5. Next chunk: D6 web desk, D7 TUI.

## Decisions already made (change only with reason)

- Queue state is derived from `review` events (assign, claim, release, escalate, defer and reopen
  keep a document in review; a resolution is the next non-`review` event). Notes, corrected values
  and evidence are `human_correction` blobs, never payloads.
- One `flock` per run (`<root>/desk/<run_id>.lock`), writer id `desk`, open, append, flush, close per
  action; the engine never writes to a document in review.
- Human decisions become `mailroom-labels/review/v1` rows with `reviewer={model:"human",
  role:"desk:<id>"}` (labels allows exactly `{model, role}` today), built by the labels importer.
- Outage causes are refused (a provider outage makes a document wait, never park).

## Findings other threads need

- mailroom-reloaded has **no terminal TUI**: `/tui` is a browser-hosted terminal in JavaScript. The
  launcher thread should pick the TUI technology knowing this.
- The sandbox Label Review Desk exists only as the zip (a claude.ai Artifact with browser-side
  state); the bundle builder is in no repo. Its decisions are already in `labels/gold-review-2026-11-18/`.
- Follow-ups (plan, "Follow-ups owned by other threads"): engine entry-point loading and wait-on-review;
  record vocabulary for `review` payload actions and appending to sealed runs; labels reviewer-id
  field and class layer; shared-tooling updates to the boundary tests, spec R9 and CI; launcher
  Compose service; The-Mailroom mirror if shapes change.
- Scratch inventories (not committed, may be gone): the old-pipeline review inventory had 19 gotchas
  (process-local lock, self-asserted reviewer, `/source` leaks the server path, restart means stateless
  resume). The plan keeps the ones that matter.


## Update 2026-11-18 (end of this thread): D1 built, reviewed, review fixes partly applied

State of D1 on this branch (all pushed): source and tests written by an Opus implementer; an independent
Opus review found 2 MAJOR and 9 MINOR (no BLOCKING); fixes were being applied when the build moved to a
solo chat. Latest gate run by the orchestrator on the pushed HEAD:
`.venv/bin/python -m pytest packages/record/tests packages/labels/tests packages/desk/tests -q -n 4` -> 988 passed; `compileall -q packages/desk/src` exit 0.

Review findings (numbers are the reviewer's):

| # | Finding | State |
|---|---|---|
| 1 | Free text could enter payloads via `requested_by`, `class_hint`, `prefill_ref` (MAJOR) | Fixed, committed (54955a9) |
| 2 | Closed-run re-check under the lock untested (MAJOR) | Test added (d157e23); mutation re-check not confirmed |
| 3 | Huge ints and deep YAML nesting escape the loaders | Fixed (341a65a) |
| 4 | Far-future timestamp breaks `due`; `enqueue` raising after a durable append; unwrapped flush OSError | Fixed (5807e77) |
| 5 | Stale index hides cases from `queue()` | Fixed (0f5193a): falls back to the log |
| 6 | Surviving mutations (reopen clears assignee, claim clears deferral, `authenticate` role check, sort_key due vs opened) | Partly pinned (last commit); not re-verified by mutation |
| 7 | `seal_guard` creates `seals/` without a durable fsync | **Not done** |
| 8 | `_run` can be enqueued | **Not done** (unverified whether the interrupted fixer did it) |
| 9 | Forked child keeps the flock: document it | **Not done** |
| 10 | Strengthen the fold-vs-index property (status, order, `_run`, no stacked review events); replace the 0.3 s sleep positive control | **Not done** |
| docs | README, `docs/test-strategy-desk.md`, plan row for the extra payload fields (`class_hint`, `confidence_hint`, `policy_name`, `policy_hash`, `prefill_ref`) | **Not done**: update for every behavior change above |

Next for D1: a fresh Opus implementer finishes items 7-10 and the docs, runs a mutation re-check on the
fixes (2, 6 and the new ones), then one more independent Opus review of the fix commits
(`git diff ef70506..HEAD`) before D1 counts as done. Open questions from the implementer: should the record
expose a public way to ask whether a run is closed (the desk relies on `seals/.lock`, pinned by a test), and
should a sample-only request for a document already in review return `None` instead of raising?
D2 to D7 are untouched. The work continues in JJB's solo chat on mailroom-dojo.

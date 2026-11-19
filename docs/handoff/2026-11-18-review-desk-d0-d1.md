# Handoff: human review desk, state after D0 and D1

Date: 2026-11-18. D0 and D1 are done (D1 after two independent reviews); D2 onward continue in JJB's solo chat on mailroom-dojo.

## Where the work is

- Repo: `Exios66/mailroom-dojo`. Branch: `claude/project-thread-rdce6z`.
- **Landing:** D1 lands on `main` through PR #9, merged from `main` and wired in the way `main` wires
  packages (Makefile order, boundary table, README, pytest path). The notes below on the stacked
  branches describe the state before that merge.
- Plan: `docs/plans/2026-11-18-review-desk-plan.md` (inventory of the old review bin, queue, resolve
  API, sandbox Label Review Desk and reloaded TUI; design; tasks D0-D7; follow-ups for other threads).
- Decision record: `docs/adr/0008-review-desk-is-event-sourced-on-the-record.md`.

## State

| Item | State |
|---|---|
| D0 plan, ADR-0008, README layout line | Done, committed |
| D1: `packages/desk/src/mailroom_desk/` (models, policy, reviewers, lock, fold, desk, errors, _yaml), `config/review/`, `packages/desk/tests/` (323 tests), `docs/test-strategy-desk.md`, root `pyproject.toml` testpaths, CircleCI and README lines | Done: built, reviewed twice independently, every finding fixed, mutation passes re-checked (see "D1 final state" below) |
| D2-D5 | Not started |
| D6 web desk UI, D7 Textual TUI | Next chunk, not started |

Baseline before desk work: `.venv/bin/python -m pytest packages/record/tests packages/labels/tests -q -n 4`
gave `678 passed` (venv: `python3 -m venv .venv && .venv/bin/pip install -e "packages/record[dev,analysis]" -e "packages/labels[dev]" fastapi httpx typer`, then `-e "packages/desk[dev]"`).

## Next steps, in order

1. D1 is done (see "D1 final state" below); nothing of it is left open except the follow-ups listed there.
2. Push the local desk commits (the orchestrator, after review).
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


## D1 final state (2026-11-18)

**Push state.** All desk commits after `d46b92f` are local on `claude/project-thread-rdce6z` when
this file is written (the first review's fixes `c4ec86b`..`14ea7dd` and the second review's fixes
after them); they are pushed by the orchestrator after review. `origin` held `d46b92f` before that
push.

**First review** (independent Opus; 2 MAJOR, 9 MINOR, no BLOCKING), all fixed:

| # | Finding | Fix |
|---|---|---|
| 1 | Free text could enter payloads via `requested_by`, `class_hint`, `prefill_ref` (MAJOR) | 54955a9: references (see the second review, (c), for what that guarantees) |
| 2 | Closed-run re-check under the lock untested (MAJOR) | d157e23 |
| 3 | Huge ints and deep YAML nesting escape the loaders | 341a65a |
| 4 | Far-future timestamp breaks `due`; `enqueue` raising after a durable append; unwrapped flush `OSError` | 5807e77, e77b0ba |
| 5 | Stale index hides cases from `queue()` | 0f5193a (superseded by the second review: reading no longer uses the index) |
| 6 | Surviving mutations (reopen, claim, `authenticate`, `sort_key`) | c4ec86b |
| 7 | `seal_guard` creates `seals/` without a durable fsync | 4764061 |
| 8 | `_run` can be enqueued | 492587a: `RunChainNotReviewable` |
| 9 | Forked child keeps the flock | d839f17: documented, deterministic fork and spawn tests |
| 10 | Weak fold-vs-index property; a sleep in a positive control | 729c16a |

**Second review** (independent; 2 MAJOR plus minors), all fixed:

| Finding | Fix |
|---|---|
| MAJOR: a broken index (a directory in place of `index.sqlite`, raw `sqlite3.OperationalError`) made `queue()` and `case()` raise | 17a437f: `queue()` and `case()` read the log only; the index is only an apply target; `index_drift()` returns `"<run>: <problem>"` lines and never raises |
| MAJOR: the index read path was slower than the log at scale (40k events, 200 cases: log 2.7 s, index 38.5 s) | 17a437f: one log scan per run; a counting-wrapper test pins one `scan` per run for `queue()` and one for `case()`, and an apply-only index proves reads never touch it. The reviewer's script now gives 2.8 s with an index |
| Tests that passed without checking their claim (shortening, the seed problem) and untested fix branches (escalate saturation, `index_drift`, append `OSError`, `_apply` on non-`ApplyRefused` errors) | 69eebdc, 17a437f |
| A reference can still hold a short label or a path-like string; "no payload field can hold free text" overclaimed | a653d64: README, `models.py` and the plan say what is guaranteed; `prefill_ref` is an opaque name that D3 must never use as a filesystem path |
| `/proc/self/fd` tests are Linux-only | 646ffd3: `LINUX_ONLY` skip marker |
| Dropping `_run` from the log-only queue survived | 3d3fc58: the property drives foreign review events on `_run` |

Mutation re-check after the second review (each applied, desk suite run, restored, `git diff`
empty): the reviewer's survivors M4, M6, M7, M9, M13, M15 and M19, and eight on the log-only read
path (per-case re-scan, ignoring the run filter, dropping `_run`, wrong sort, folding only the
first document, drift ignoring documents new to the log, drift raising on a damaged log, `case()`
ignoring `now`): all 15 caught. The failing tests are listed in `docs/test-strategy-desk.md`.

**Gate on the final tree** (clean, after the commit that adds this file; this commit is docs
only): `.venv/bin/python -m pytest packages/desk/tests -q -n 4` -> 323 passed (three runs) and
serially 323 passed; `.venv/bin/python -m pytest packages/record/tests packages/labels/tests packages/desk/tests -q -n 4`
-> 1001 passed; `.venv/bin/python -m compileall -q packages/desk/src` exit 0. No linter or type
checker is configured.

**Follow-ups for the record thread** (open questions, not done here):

1. A public "is this run closed" API (and a closing lock): the desk applies `verify`'s rule (an
   experiment run with a `seals/<n>.json`) and takes `runs/<run_id>/seals/.lock` from the
   documented layout, pinned by a test (plan follow-up 2).
2. `seal_run` should always fsync the run directory: it creates `seals/` through
   `blobs._makedirs_durable`, which skips the directory and its parent when `seals/` already
   exists, so a `seals/` created by someone else is never made durable by `seal_run`. The desk's
   `seal_guard` fsyncs both on every call as a workaround.

Other open question from D1: should a sample-only request for a document already in review
return `None` instead of raising `AlreadyInReview`? D2 to D7 are untouched.

## Meshing with main (checked 2026-11-18 15:50 UTC against origin/main e07c471)

`main` now holds #2 (record, squashed, with fixes since this branch's copy) and #6 (shared libs:
`packages/common`, `packages/dictionaries`, ADR-0007 shared-leaf-packages). This branch still carries the
**older** record (26xcn6) and labels (pbvxf9) trees, so a plain `git merge origin/main` conflicts in about
50 add/add files. Do not resolve those by hand.

- **Desk against main's record: verified.** In a scratch worktree at `origin/main` with the labels branch
  and this branch's `packages/desk` + `config/review` copied in: `PYTHONPATH=packages/desk/tests:packages/record/tests:packages/labels/tests
  python -m pytest packages/desk/tests -q -n 4` -> 322 passed, 1 failed. The failure was a test that
  expected sqlite's raw `OperationalError` text; main's record translates it to `IndexUnavailable`.
  Fixed on this branch (the test now accepts either); desk suite 323 passed. The desk therefore works on
  the new record unchanged, including `seals/.lock` and the index apply path.
- **ADR number collision fixed:** main already has `0007-shared-leaf-packages.md`, so the desk's ADR is now
  `docs/adr/0008-review-desk-is-event-sourced-on-the-record.md` and every reference says ADR-0008.
- **Desk depends on labels** (`policy.py` reads the client profile), so the desk PR must stack on the
  labels PR (#5) or wait for it to merge.
- **Recipe to make a clean PR once #5 is on main:** `git fetch origin`; in a worktree at `origin/main`,
  copy `packages/desk`, `config/review`, `docs/test-strategy-desk.md`, `docs/plans/2026-11-18-review-desk-plan.md`,
  `docs/handoff/2026-11-18-review-desk-d0-d1.md`, `docs/adr/0008-*.md` from this branch; hand-apply the small edits
  to `README.md` (layout line for `packages/desk`), `docs/adr/README.md` (row 0008), root `pyproject.toml`
  (desk tests in `testpaths`/`pythonpath`) and `.circleci/config.yml` (install and run desk tests); check
  whether main's shared libs (`packages/common`, `packages/dictionaries`) should replace anything in the
  desk (for example its own YAML loader, name rules or reason vocabularies) before merging; run the full
  suite; then on this branch `git merge -s ours origin/main`, copy that tree in as a new commit, and push
  (a fast-forward, no force). The PR diff is then only the desk.

State of this branch after this update: HEAD pushed; desk suite 323 passed; full run on the old
record+labels trees 1001 passed; 13 desk commits plus the ADR rename and the test fix.

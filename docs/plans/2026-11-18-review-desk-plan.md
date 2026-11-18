# Project 7: the human review desk

Serves R1 (auditability, less human review), R4, R5, R6, R7, R8, R9 and R10 of
`docs/specs/2026-11-18-lean-rewrite-design.md`. Package: `packages/desk/` (`mailroom_desk`).
Decision record: `docs/adr/0007-review-desk-is-event-sourced-on-the-record.md`.

JJB, 2026-11-18: "WE NEED TO ADD THE FULL HUMAN REVIEW DESK INTEGRATION AS WELL TOO PLEASE, AS ITS
OWN NODE, AS IN THE PREVIOUS MAILROOM PIPELINE— BUT ENHANCING IT FURTHER, AND ROLLING IT INTO THIS
mailroom-dojo singular WORKSPACE , so that the human review tray can be managed from this repository
alone with everything else too."

## Goal

The whole review tray is managed from this workspace: documents enter review by configured rules,
are assigned and claimed, are reviewed in a CLI, an HTTP API (and, in the next chunk, a web desk and
a TUI), and every outcome is written to the run record as events and blobs and becomes a gold label
candidate carrying the reviewer's identity. The desk has no LLM, names no provider and no document
class, and fits the HTTP API, Docker Compose and Modal launch paths because it only needs a record
root directory.

## What exists today (inventory, verified in code by read-only passes on 2026-11-18)

| Part | Today | Gap the desk closes |
|---|---|---|
| Park node | llm-mailroom `human_review_node` (`graph/build_graph.py`) parks a file in the review bin, writes a manifest, a catalog row and an audit entry, then `interrupt()`s. About 23 cause tokens built by `pipeline/reconsideration.py`; thresholds in `taxonomy.yaml`. | Causes and thresholds become a review policy file. Spot-check sampling added. Outage causes refused. |
| Queue | No table: catalog `stage=="review"` plus manifests. Priority and SLA computed on read (`review_priority.py`; high 4 h, normal 24 h, low 72 h). No assignment; a process-local lock gives HTTP 409. | A queue folded from the record, with assignment, claim, lease, escalate, defer, reopen. Priority and SLA decided at enqueue and recorded. |
| Resolve | `POST /review/{id}/resolve`: `approve_as_is`, `correct`, `reclassify`, `reject`, plus dispositions `record` and `requeue`; `field_corrections`; reviewer self-asserted (`X-Reviewer`, default `human_reviewer`). | Reviewer registry and authenticated identity; corrections stored as a new document version (blob) in the chain. Old request shape kept as a compatibility route. |
| After resolve | archive route, reextract route (enters at `extract`), reject route (to `failed/`). Corrections in `manifest.human_label`; used for calibration only (`export_review_labels.py`). | The disposition is an event the engine routes on. Corrections become label candidates in the labels package. |
| Audit | Hash-chained audit log; entries best-effort; restart gives a stateless resume and an incomplete trace on reject. | The record is the audit: every desk action is an event in the document's hash chain; `verify` covers it. |
| Sandbox Label Review Desk | Single-page app, 3 panes, evidence highlighting, policy-group bulk actions, progress, filters, j/k/a keys. Lives only in the zip (a claude.ai Artifact using `window.claude.use('db')`); the bundle builder is in no repo. No export, no reviewer identity, no audit trail, free-text corrections, field-level only. | Rebuilt as the desk's web UI (next chunk) on the same case data, with structured corrections, evidence quotes, identity and export. Its 8 desk decisions and the owner and audit decisions already live in `labels/gold-review-2026-11-18/`. |
| mailroom-reloaded TUI | **Not a terminal TUI.** `/tui` is a browser-hosted terminal emulator in vanilla JS (`api/tui/`, ~7.5k lines); its `review`/`resolve` commands call `/v1/review/{id}/resolve`. No textual, rich or curses in code. | The launcher project must decide the TUI technology knowing this. The desk's terminal front end is a CLI now and a Textual TUI in the next chunk, both over the same Python API. |

## Design

### The desk is a node and a package

- **Node.** `mailroom_desk.node` registers the node type `human_review` through the entry-point group
  `mailroom.nodes`. It takes a typed `ReviewRequest` (document, causes, who asked, blobs to show) and
  returns a `Disposition` (`archive`, `reextract`, `reject`, `requeue`, `record_only`) as a partial
  state update, the same contract as every other node. The engine loads registered nodes generically,
  so `engine` never imports `desk` (R9). The graph shows the node; the policy wires it.
- **Package.** `mailroom_desk` imports `mailroom_record` and `mailroom_labels` and nothing else of
  ours (boundary test). The launcher composes it (CLI, API mount, UI).

### State lives only in the record (ADR-0007)

Queue state is a fold over a document's `review` events since its last non-review event (an
*episode*). Nothing is stored beside the log. Event shape, using only fields `mailroom_record` already has:

| Action | Event | In queue after? |
|---|---|---|
| enqueue | `kind="review"`, `node="human_review"`, `cause=<primary cause>`, `payload={action:"enqueue", causes, rule, priority, sla_hours, queue, required_role, prefill_ref, sampled, policy_name, policy_hash, requested_by, class_hint, confidence_hint}`, `inputs` = blobs shown. D1 extended the first draft (`causes, rule, priority, sla_hours, queue, requested_by, sampled?`) with `required_role`, `prefill_ref`, `policy_name`, `policy_hash`, `class_hint` and `confidence_hint`, so the event records everything the routing read and decided. `requested_by`, `class_hint` and `prefill_ref` are references (ASCII letters, digits and `_ . : - /`, at most 128 characters), never free text | yes |
| assign, claim, release, escalate, defer, reopen, comment-free state changes | `kind="review"`, `payload={action, actor, ...}` | yes |
| resolve (approve_as_is, correct, reclassify) | `kind="output"`, `decision={resolution, route}`, `outputs=[human_correction blob]`, `payload={reviewer, ...}` | no |
| resolve (reject) | `kind="decision"`, `cause=<reject reason token>`, `outputs=[human_correction blob]` | no |

This matches the record index's rule (a document is in review exactly when its head event is a
`review`), so `Index.review_queue()` and the desk's fold must agree; a test asserts it on every
generated history. Free text (notes, corrected values, evidence quotes) is a `human_correction`
blob, never an event payload (the record forbids document text in payloads).

### Concurrency

The record forbids two writers on one document chain (`ForkedChain`). `DocumentInUse` guards only
the same process, so the desk serialises its own writes with one `flock` per run
(`<root>/desk/<run_id>.lock`), writer id `desk`, and opens, appends, flushes and closes per action
(a long-lived writer would cache a stale head). Each action re-reads the case state under the lock
and refuses with a typed error if it no longer holds (`AssignedToOther`, `AlreadyResolved`,
`StaleCase`). The engine must not write to a document that is in review; the node contract makes
the engine wait for the disposition. A sealed experiment run is refused (`RunClosed`); production
service-period runs stay open.

### Routing rules into review (config, not code)

`config/review/<name>.yaml`, validated, hashed and recorded on the enqueue event (`rule`):

- a cause catalog: token, description, default priority, SLA hours (carried over from llm-mailroom's
  `review_queue` block; domain-free: a token is `[a-z0-9_:]+`);
- rules: `when` (cause in, class in, confidence below) `then` (priority, SLA, queue, required role,
  prefill reference). First match wins; a final default rule is required;
- deterministic spot-check sampling: `sample: {rate, seed}` over the document hash, so a QA sample
  of auto-archived documents enters the same queue with `sampled=true`;
- outage tokens (`transient_exhausted`, `provider_unavailable`) are refused: a provider outage
  makes a document wait, never park (R1).

### Identity and audit

`config/review/reviewers.yaml` lists reviewers (id, initials, roles, optional sha256 of an API
token). Every desk event records `reviewer={id, role, identity: "authenticated"|"asserted"}`. A
registry in strict mode refuses unknown reviewers; open mode (dev, tests) records `asserted`. The
audit trail is the chain: `desk audit <doc>` prints who did what and when, and `desk verify`
wraps `mailroom_record.verify`.

### Decisions become label candidates

The correction blob (`mailroom-desk/correction/v1`) holds per-field verdicts in the labels review
shape: `{field, verdict: accept|correct, entry: {value, evidence?, absent?}, note?}`, plus the class
decision, the reviewer and the versions shown. Only fields the reviewer confirmed or corrected are
candidates (a bulk "confirm shown fields" action lists them explicitly; nothing is confirmed by
omission). `desk export-labels` scans resolution events, writes `decisions.jsonl` rows in
`mailroom-labels/review/v1` with `reviewer={model:"human", role:"desk:<id>"}` and builds a review
snapshot with `mailroom_labels.importers.review.build_review`, so human decisions are `gold`
(`review:gold`) with the reviewer recorded and the labels precedence is untouched. The export is
derived and repeatable: the record stays the truth. Class decisions are listed as not exported until
the labels package has a class layer (follow-up).

### Surfaces

CLI `mailroom-desk` (typer): `enqueue`, `queue`, `show`, `assign`, `claim`, `release`, `escalate`,
`defer`, `reopen`, `resolve`, `audit`, `verify`, `export-labels`, `rules check`. HTTP API (FastAPI,
optional extra) with `/v1/review/queue` and `/v1/review/{id}/resolve` in the old shape (The-Mailroom
and the sandbox `review_cli` keep working), and new routes for case view, assign, claim, audit.
Auth: bearer token mapped to a reviewer; loopback-only when none configured (as today).

## Tasks

Each task: a fresh Opus (medium or higher) implementer, then a separate fresh Opus reviewer; the
orchestrator re-runs the gates. Tests exercise real record directories, real processes and real
failures (R8); `docs/test-strategy-desk.md` has the matrix.

| Task | Content | This chunk |
|---|---|---|
| D0 | This plan, ADR-0007, test strategy, README layout line | yes |
| D1 | Package skeleton; models; review policy and reviewer registry (load, validate, hash); `enqueue` with rules and sampling; queue fold; agreement with `Index.review_queue()` | yes |
| D2 | assign, claim, release, lease expiry, escalate, defer, reopen; resolve (4 resolutions) with correction blob, validation against the client profile, optimistic concurrency; multi-process tests | yes |
| D3 | Case view (version chain by blob role, capped text, no server path); `export-labels` through `build_review`; round trip through `mailroom_labels.resolve` shows gold with the reviewer | yes |
| D4 | `human_review` node, `Disposition`, entry-point registration, a fake engine loop test (park, wait, resume) over a real record; CLI | yes |
| D5 | HTTP API with old-shape compatibility routes and token identity | yes |
| D6 | Web desk UI (rebuild of the sandbox desk on case data, evidence quotes, structured edits, identity, audit panel) | next chunk |
| D7 | Terminal TUI (Textual) and Compose wiring example | next chunk |

## Follow-ups owned by other threads (not edited here)

1. Engine: load nodes from the entry-point group `mailroom.nodes`; route on `Disposition`; wait, never write, while a document is in review; outage means wait.
2. Record: a decision on whether `kind="review"` payload actions need a registered vocabulary; appending to a sealed run (the desk refuses it). The record has no public "is this run closed" call or closing lock: the desk applies `verify`'s rule (an experiment run with a `seals/<n>.json`) and, for an experiment run, holds `runs/<run_id>/seals/.lock` (the lock `seal_run` takes) between its check and its append; a test pins that layout. A public API would remove the coupling.
3. Labels: an explicit reviewer id field (today `reviewer` is exactly `{model, role}`, so the desk uses `role="desk:<id>"`); a class layer for human class decisions.
4. Shared tooling: add `mailroom_desk` to the `WORKSPACE` tuple of the record and labels boundary tests and the dependency rule line in `README.md` and spec R9; add the desk tests to the root `pyproject.toml` `testpaths` and `.circleci/config.yml` (this PR makes the minimal edits it needs and says so).
5. Launcher: the TUI technology decision (mailroom-reloaded has a browser terminal, not a terminal TUI); Compose service for `mailroom-desk serve` sharing the record volume.
6. Downstream: The-Mailroom schema mirror if event or route shapes change (compat routes keep the old shape).

## Not in this project

The upload route, the engine's graph and policies, label snapshots other than the desk's own review snapshot, scoring of review outcomes (the Dojo reads the events; it needs `kind="review"` payload actions to be stable, hence follow-up 2).

# mailroom-desk

The human review desk of the Mailroom (`mailroom_desk`), event-sourced on the run record
(ADR-0008, plan `docs/plans/2026-11-18-review-desk-plan.md`). Documents enter review by
configured rules; the queue is a fold over each document's `review` events, so the desk
stores nothing beside the record. It imports `mailroom_record` and `mailroom_labels` and
nothing else of the workspace, and names no document class, field or pipeline node.

Status: task D1 (models, review policy, reviewer registry, `enqueue`, the queue fold).
Assign, claim, resolve, export, the `human_review` node, the CLI and the HTTP API are
tasks D2 to D5.

From the workspace root (`make install` installs every package; this installs only the
desk and what it needs, editable, because `mailroom-record` and `mailroom-labels` are not
published on PyPI):

```bash
python3 -m venv .venv && .venv/bin/pip install -e packages/record -e packages/labels -e "packages/desk[dev]"
.venv/bin/python -m pytest packages/desk/tests -q
```

## Use

```python
from mailroom_desk import Desk, ReviewRequest, load_policy

policy = load_policy("config/review/default.yaml")
desk = Desk("<record root>")            # optional: index=Index(...), clock=..., lock_timeout=...
done = desk.enqueue(ReviewRequest(run_id="r1", doc_id="<doc sha256>",
                                  causes=["sorter_low_confidence"], requested_by="node:classify"), policy)
desk.queue("r1")                          # cases in review, in queue order (read from the log)
desk.case("r1", "<doc sha256>")           # one document's case (its last episode)
desk.index_drift()                        # with an index: problem lines, [] when it agrees with the log
```

## Modules

| Module | What it holds |
|---|---|
| `models.py` | `ReviewRequest`, `ReviewerRef`, the action payloads (`enqueue`, `reopen`, `assign`, `claim`, `release`, `escalate`, `defer`) and `parse_payload`, `CaseState` |
| `policy.py` | `load_policy` / `parse_policy`: cause catalog, rules (first match wins, final default required), spot-check sample, outage refusal, the policy hash |
| `reviewers.py` | `load_registry` / `parse_registry`: strict or open mode, roles, token identity, the registry hash |
| `fold.py` | `fold_case`: a document's case from its events (episode, leases, deferrals, SLA) |
| `lock.py` | `desk_lock` (per run, `<root>/desk/<run_id>.lock`) and `seal_guard` (the record's seal lock; makes `seals/` durable first) |
| `desk.py` | `Desk`: `enqueue`, `case`, `queue`, `index_drift`, `run`, `is_closed` |
| `errors.py` | one typed error per refusal (`AlreadyInReview`, `RunClosed`, `OutageNotReviewable`, ...) |

## Rules worth knowing

- A document is in review exactly when its head event is a `review` event (the record
  index's rule). An *episode* is the review events since the document's last non-review
  event; a resolution is the next non-review event.
- Every desk append happens under the run's desk lock, with the writer id `desk`, opened,
  flushed and closed per action. For an experiment run the desk also holds the record's
  seal lock, so a seal cannot land between the "is this run closed?" check and the append.
- The desk lock serializes desk actions only; it does not lock out other record writers.
  The record leaves document ownership across processes to its callers (`mailroom_record.log`,
  "One owner per document"), so the contract is: the engine flushes and closes its writer
  for a document before it hands the document to the desk, never writes to a document in
  review, and opens a fresh writer when processing resumes (an open writer caches a stale
  head) (plan follow-up 1). In one process a live writer holding the document makes
  `enqueue` raise `RecordBusy`. Across processes a broken contract forks the chain, and the
  fork is caught, not prevented: `verify_run` reports `forked_chain`, and `enqueue`, `case`
  and `queue` raise `RecordDamaged` (a process test breaks the rule on purpose).
- Never fork a process while it holds a desk lock: a `flock` belongs to the open file
  description, which `fork` shares, so a child forked without `exec` keeps the lock held
  after the parent releases it, until the child exits. Start workers with
  `multiprocessing`'s `spawn` context or `subprocess` (fork then exec): the desk's lock
  descriptors are not inheritable, so those children never hold a lock.
- A pruned run (packed, then `prune_run`) keeps its `run.json`, but its events are in its
  archive: `queue()` over every run skips it, and `queue(run_id)`, `case` and `enqueue` naming
  it raise `RunArchived`. Unpack the archive (`mailroom-record unpack`) to review it again.
- A sealed experiment run is refused (`RunClosed`); a `service_period` run is never closed.
  The closed check is repeated under the locks, so a seal that lands while an enqueue waits
  for the desk lock is still refused.
- `seal_guard` creates `runs/<run_id>/seals/` if it is missing and fsyncs it and the run
  directory on every call, because `seal_run` skips those fsyncs for a directory that
  already exists.
- The run-level chain `_run` holds run events, not a document: `enqueue` refuses it
  (`RunChainNotReviewable`), with causes or as a sample-only request.
- The log is the truth, and reading never needs the index: `queue()` folds each run from
  one ordered scan of its log (one scan per run, however many cases are in review) and
  `case()` from one scan. An index given to the desk only receives the desk's events
  (`Enqueued.index_error` says why it refused one), so a stale, locked or broken index
  changes no answer. `Desk.index_drift()` is a diagnostic that never raises: it returns
  `"<run_id>: <problem>"` lines (unknown run, index or log unreadable, document heads that
  differ), and `Index.reindex` repairs the index.
- Outage tokens (`transient_exhausted`, `provider_unavailable`) are refused in policies and
  requests: an outage makes a document wait, never park (R1).
- `enqueue` never raises after its event is durable: an `OSError` from closing the writer
  after a good flush is ignored, an index that refuses the apply is reported in
  `Enqueued.index_error`, and an SLA due time past 9999-12-31 saturates and is listed as an
  anomaly. A failed write or fsync raises `RecordWriteFailed` (the cause is the `OSError`;
  the event may be on disk, so read the case again before retrying); a read failure while
  appending is `RecordDamaged`. Times are written with a four-digit year (year 5 is `0005`).
- A forked chain (two stored events with one `seq` of a document) is `RecordDamaged` (the
  cause is the record's `ForkedChain`) for `enqueue`, `case` and `queue` alike: the record's
  `iter_events` returns both sides of a fork, and the desk never folds them into a case.
- Hostile config files are typed errors, never crashes: integers too large for a float,
  integer literals too long to read and YAML nested thousands deep are a `PolicyError` or
  `RegistryError`, and problem messages show refused values shortened.
- Leases, deferrals and SLA breach are judged when read, against the desk clock
  (injectable). A lease holds while `now < lease_expires_at`; an SLA is breached when
  `now > sla_due`.
- Free text never goes in a payload: what the reviewer is shown is a list of blob
  references (`inputs`), and those blobs must be present before the event is written.
- `requested_by`, `class_hint` and `prefill_ref` are references: ASCII letters, digits and
  `_ . : - /`, starting with a letter or digit, at most 128 characters. What that
  guarantees: no whitespace, no control, format or bidi character, nothing outside ASCII,
  and a bounded length, so no sentence, note or pasted document fits. It does not stop a
  short label (`patient-x-has-condition`) or a path-like string (`a/../../etc`): callers
  put names there, never facts about a person, and every free text goes in a blob. A
  payload can never be erased, so this matters.
- `prefill_ref` is an opaque name. D3 (and any later reader) must look it up by name and
  must never use it as a filesystem path or join it to one.

Test strategy: `docs/test-strategy-desk.md`.

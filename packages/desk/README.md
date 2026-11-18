# mailroom-desk

The human review desk of the Mailroom (`mailroom_desk`), event-sourced on the run record
(ADR-0007, plan `docs/plans/2026-11-18-review-desk-plan.md`). Documents enter review by
configured rules; the queue is a fold over each document's `review` events, so the desk
stores nothing beside the record. It imports `mailroom_record` and `mailroom_labels` and
nothing else of the workspace, and names no document class, field or pipeline node.

Status: task D1 (models, review policy, reviewer registry, `enqueue`, the queue fold).
Assign, claim, resolve, export, the `human_review` node, the CLI and the HTTP API are
tasks D2 to D5.

```bash
pip install -e packages/record -e packages/labels -e "packages/desk[dev]"
python -m pytest packages/desk/tests -q
```

## Use

```python
from mailroom_desk import Desk, ReviewRequest, load_policy

policy = load_policy("config/review/default.yaml")
desk = Desk("<record root>")            # optional: index=Index(...), clock=..., lock_timeout=...
done = desk.enqueue(ReviewRequest(run_id="r1", doc_id="<doc sha256>",
                                  causes=["sorter_low_confidence"], requested_by="node:classify"), policy)
desk.queue("r1")                          # cases in review, in queue order
desk.case("r1", "<doc sha256>")           # one document's case (its last episode)
```

## Modules

| Module | What it holds |
|---|---|
| `models.py` | `ReviewRequest`, `ReviewerRef`, the action payloads (`enqueue`, `reopen`, `assign`, `claim`, `release`, `escalate`, `defer`) and `parse_payload`, `CaseState` |
| `policy.py` | `load_policy` / `parse_policy`: cause catalog, rules (first match wins, final default required), spot-check sample, outage refusal, the policy hash |
| `reviewers.py` | `load_registry` / `parse_registry`: strict or open mode, roles, token identity, the registry hash |
| `fold.py` | `fold_case`: a document's case from its events (episode, leases, deferrals, SLA) |
| `lock.py` | `desk_lock` (per run, `<root>/desk/<run_id>.lock`) and `seal_guard` (the record's seal lock) |
| `desk.py` | `Desk`: `enqueue`, `case`, `queue`, `index_drift`, `run`, `is_closed` |
| `errors.py` | one typed error per refusal (`AlreadyInReview`, `RunClosed`, `OutageNotReviewable`, ...) |

## Rules worth knowing

- A document is in review exactly when its head event is a `review` event (the record
  index's rule). An *episode* is the review events since the document's last non-review
  event; a resolution is the next non-review event.
- Every desk append happens under the run's desk lock, with the writer id `desk`, opened,
  flushed and closed per action. For an experiment run the desk also holds the record's
  seal lock, so a seal cannot land between the "is this run closed?" check and the append.
- A sealed experiment run is refused (`RunClosed`); a `service_period` run is never closed.
- With an index, `queue()` checks run by run that the index's document heads equal the log's.
  A run where they differ (or an unavailable index) is read from the log and a warning is
  logged (logger `mailroom_desk`), so a stale index never hides a case;
  `Desk.index_drift()` lists such runs and `Index.reindex` brings them back in step.
- Outage tokens (`transient_exhausted`, `provider_unavailable`) are refused in policies and
  requests: an outage makes a document wait, never park (R1).
- `enqueue` never raises after its event is durable. A failed write or fsync raises
  `RecordWriteFailed` (the cause is the `OSError`; the event may be on disk, so read the case
  again before retrying). An SLA due time past 9999-12-31 saturates and is listed as an
  anomaly instead of raising.
- Leases, deferrals and SLA breach are judged when read, against the desk clock
  (injectable). A lease holds while `now < lease_expires_at`; an SLA is breached when
  `now > sla_due`.
- Free text never goes in a payload: what the reviewer is shown is a list of blob
  references (`inputs`), and those blobs must be present before the event is written.
- `requested_by`, `class_hint` and `prefill_ref` are references: ASCII letters, digits and
  `_ . : - /`, at most 128 characters, no whitespace or control characters. A payload can
  never be erased, so no payload field can hold free text.

Test strategy: `docs/test-strategy-desk.md`.

# ADR-0007: The review desk is event-sourced on the run record

**Status:** Accepted
**Date:** 2026-11-18
**Deciders:** JJB (requirement: the human review tray is managed from this workspace alone, as its
own node, enhanced beyond the previous pipeline)

## Context

The previous pipeline kept the review tray in three places that could disagree: a bin on disk, a
catalog row and a manifest, plus a best-effort audit log. It had no assignment, a process-local
lock, a self-asserted reviewer, and corrections stored in a manifest that never reached the labels.
The sandbox's Label Review Desk kept its marks in a browser and lost most of them.

The record already provides what a desk needs: per-document hash chains, an event kind `review`,
a blob role `human_correction`, and an index rule that a document is in review exactly when its head
event is a `review`.

## Decision

- Queue state is derived from the document's events; the desk stores nothing beside the log.
  Assignment, claim, release, escalation, deferral and reopening are `review` events (the document
  stays in the queue); a resolution is the next non-`review` event (it leaves).
- Notes, corrected values and evidence are a `human_correction` blob, never a payload.
- Writes are serialised per run with an exclusive file lock and a single writer id, and each action
  re-reads the case under the lock. The desk never keeps a writer open.
- Routing rules, cause catalog, SLAs, sampling and reviewers are configuration under
  `config/review/`; the policy hash is recorded on each enqueue.
- A human decision becomes a label candidate by export: resolution events are turned into
  `mailroom-labels/review/v1` rows with a human reviewer and built into a review snapshot by the
  labels importer. The export is derived; the record stays the truth.
- The desk is a node registered through an entry point; the engine does not import it.

## Options considered

- **A review table in the index database.** Rejected: the index is disposable and rebuilt from the
  log (ADR-0001); state that lives only there could be lost.
- **A separate desk database or JSONL ledger.** Rejected: a second truth that can disagree with the
  record, and a second thing to verify.
- **Appending desk events through the engine's writer.** Rejected: the engine does not exist yet,
  and a shared writer couples the desk to it.

## Consequences

- Every desk action is in the document's hash chain and is covered by `verify`; "who changed what"
  is answered from the record alone.
- The desk's fold and `Index.review_queue()` must agree; a property test enforces it.
- Free text lives in blobs, so tombstoning a blob removes content but keeps the audit event.
- Writing to a sealed experiment run is refused; deployments use service-period runs.
- Reviewer identity is only as strong as the registry mode: `asserted` events are marked as such.

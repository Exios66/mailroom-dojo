# ADR-0002: One hash chain per document, committed by chained seals

**Status:** Accepted
**Date:** 2026-11-18
**Deciders:** JJB (design approval); the per-document chain is a call listed in the design

## Context

The current audit log keeps one global hash chain, so parallel documents queue on one lock.
The record must still make tampering detectable: an edited event, a deleted event, a deleted
document and a cut tail must all fail verification. A production service never ends, so a
run-level hash computed "at the end" would never exist.

## Decision

- Each document has its own chain inside a run: `seq` starts at 0, the first event's
  `prev_hash` is `GENESIS` (64 zeros), each event's `hash` is the SHA-256 of the canonical
  JSON of every field except `hash`. The run-level pseudo-document is `_run`.
- A seal commits the head of every document chain and the list of documents, plus the event
  count, and chains to the previous seal (`prev_seal_hash`). Experiment runs seal once at the
  end; service periods seal on a timer. Sealing with nothing new is a no-op.
- `verify_run` recomputes both levels and names the first broken entry by kind:
  `bad_hash`, `broken_link`, `seq_gap`, `seal_mismatch`, `document_missing_from_log`,
  `truncated_after_seal`, `missing_blob`; `torn_tail` is reported but does not fail.

## Options considered

### A. Per-document chains + chained seals (chosen)
| Dimension | Assessment |
|---|---|
| Complexity | Medium: two levels to verify |
| Cost | One hash per event, one small file per seal |
| Scalability | Documents never wait on each other |
| Team familiarity | Medium |

**Pros:** no shared lock; deleting a whole document or cutting a tail is caught by the seal.
**Cons:** between seals, a deleted whole document chain is not detectable (bounded by the
seal period).

### B. One global chain per run
**Pros:** a single head proves everything. **Cons:** the lock this design exists to remove.

### C. Merkle tree over all events at seal
**Pros:** compact inclusion proofs. **Cons:** more code than any consumer needs; per-document
chains already give the ordered history a reviewer reads.

## Trade-off analysis

A accepts a detection window equal to the seal period for whole-document deletion, in
exchange for lock-free parallel appends. The period is the engine's choice (hourly in the
design), and every experiment run seals at its end, so stored runs have no window.

## Consequences

- Easier: parallel writers, per-document audit trails, version chains.
- Harder: verify must merge segments and check seals; the seal must list documents, not only
  heads.
- Revisit when: an external auditor needs inclusion proofs for single events.
- Sealing re-hashes and re-links every line it exports, so a seal never commits over a
  tampered or unlinked line (`SealRefused`); lines an earlier seal covered are checked by
  verify, not re-hashed on every seal.
- The seal file is written before its Parquet column file. A lost column file (of any
  seal) is rebuilt from the log by the next `seal_run`. A column file whose seal file is
  gone can only mean the seal was deleted, so `seal_run` refuses rather than re-sealing
  under a new hash, and verify reports `seal_missing`. If both files of the latest seal are
  deleted, nothing on disk shows it existed: only a copy of the seal hash kept elsewhere
  (an index, a published report) can show that deletion.

## Action items

1. [ ] Task 5: chains per document, `prev_hash`/`seq` set by the writer.
2. [ ] Task 6: seals with `doc_heads`, `document_ids`, `event_count`, `prev_seal_hash`.
3. [ ] Task 7: one tamper test per problem kind.

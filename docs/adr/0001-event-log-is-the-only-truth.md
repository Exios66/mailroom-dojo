# ADR-0001: The event log is the only source of truth

**Status:** Accepted
**Date:** 2026-11-18
**Deciders:** JJB (design approval, 2026-11-18); builder calls per the design's "Left to the builder"

## Context

Today the facts about one run are spread over the pipeline's audit table, the sandbox ledger
and run folders, the experiment log and the trace sink. They disagree after crashes, and
records get lost. The design's systems review found that an event log plus a live database,
both written, can disagree after a crash.

Forces: durability (a crash loses at most the step in flight), auditability (any decision can
be rebuilt from the record alone), speed (no global lock), and a small team that cannot
operate much infrastructure.

## Decision

The append-only event log is the only truth. Everything else is derived from it and can be
rebuilt with one command: the SQLite index (`reindex`), the Parquet column files (written at
seal from log events), traces and version chains. The write order is fixed: blob, then the
event that names it, then the index. `LogWriter.append` refuses an event whose blobs are not
stored (`BlobNotStored`). Nothing is written to the index that is not already in the log.

## Options considered

### A. Log as truth, derived index (chosen)
| Dimension | Assessment |
|---|---|
| Complexity | Low: one writer path, one rebuild path |
| Cost | Rebuild time grows with history; fine at ~200k events/day |
| Scalability | Single host; enough per the design's load estimate |
| Team familiarity | High: files and SQLite |

**Pros:** a crash can never leave two truths; the index is disposable; tests can prove
`reindex == incremental`. **Cons:** queries that need the full history go to column files,
not the live index.

### B. Database as truth (SQLite or Postgres tables for events)
**Pros:** ad-hoc SQL on live data. **Cons:** today's slow-database pain came from this shape
(a global chain lock and many writes per step); content and audit data share one failure
domain; harder to pack and move a run.

### C. Dual write (log and database, both authoritative)
**Pros:** none that A does not give. **Cons:** the crash-disagreement the review flagged.

## Trade-off analysis

A gives up live SQL over full history, which no gate needs: analysis reads sealed column
files. In return a crash has one well-defined effect (a torn last line, see ADR-0004) and the
index is never a source of loss.

## Consequences

- Easier: crash recovery, moving or packing a run, testing derived state for equality.
- Harder: anything needing joins over live, unsealed history must read segments.
- Revisit when: more than one host must write the same run, or reindex time becomes an
  operational problem.

## Action items

1. [x] Plan tasks 5 (log), 8 (index with `reindex` and `apply`), 6 (Parquet from the log).
2. [x] Test: index built incrementally equals index rebuilt from the log (Task 8:
   `test_reindex_equals_incremental`, and the property
   `test_incremental_equals_reindex_over_random_histories`).
3. [x] Test: deleting the index file and reindexing gives the same answers (Task 8:
   `test_index_is_disposable`, `test_a_damaged_or_old_index_is_refused_then_rebuilt`).

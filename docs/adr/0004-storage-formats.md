# ADR-0004: JSONL segments, Parquet at seal, SQLite index, DuckDB for analysis

**Status:** Accepted
**Date:** 2026-11-18
**Deciders:** builder call within the design ("Exact record schema, file formats and table
layouts" are left to the builder)

## Context

The design's load estimate is ~200,000 events a day (~200 MB raw) at 10,000 documents a day:
small. The throughput floor is 249 documents a minute with durable per-event writes on, on
one host. Analysis must re-score any stored run in minutes on a laptop without touching the
live database.

## Decision

- **Live events:** one JSONL segment file per writer per run
  (`runs/<run_id>/segments/<writer>.jsonl`), one canonical JSON object per line, appended
  under a per-writer lock and fsynced every `flush_every` events. Processes never share a
  file, so lines cannot interleave. A crash leaves at most one torn last line, which readers
  skip and report (`torn_tails`) and the next writer continues the chain past it.
- **Sealed events:** `runs/<run_id>/columns/events-<n>.parquet`, the events since the
  previous seal, every field except `payload` (`decision` as a JSON string, blob refs as
  hash lists).
- **Index:** one SQLite file, WAL mode, `synchronous=NORMAL`, tables `runs`, `documents`,
  `doc_runs`, `chain_heads`, `review_queue`; no text, no payloads. Behind one `Index` class so
  a Postgres twin can be added for the deploy.
- **Analysis:** DuckDB over the Parquet files, as an optional extra (`analysis`).
- **Archive:** a sealed run packs into one `.tar.zst` with a manifest of member hashes.

## Options considered

### A. JSONL + Parquet + SQLite (chosen)
| Dimension | Assessment |
|---|---|
| Complexity | Low; all stdlib or one wheel each |
| Cost | No services |
| Scalability | Many times the estimated load on one host |
| Team familiarity | High |

**Pros:** appends are cheap and crash-safe; columns are fast to scan; the index is tiny.
**Cons:** three formats; Parquet only exists after a seal.

### B. SQLite for everything
**Pros:** one file. **Cons:** writer contention and the coupling ADR-0001 rejects.

### C. DuckDB as the live store
**Pros:** fast analysis. **Cons:** single-writer process model, weaker crash semantics for an
append log, larger dependency on the hot path.

### D. A broker (Kafka, NATS) or a warehouse
Rejected by the design's systems review: the data is small; the limit is the model
provider's rate limit.

## Consequences

- Easier: crash recovery, laptop analysis, packing.
- Harder: readers merge several segment files and order by `(doc_id, seq)`.
- Revisit when: more than one host writes one run, or Postgres is needed for the deploy's
  live index.

## Action items

1. [ ] Task 5: per-writer segments, torn-tail handling, thread and process parallel tests.
2. [ ] Task 6: Parquet export without `payload`.
3. [ ] Task 8: SQLite index; Task 11: pack format.

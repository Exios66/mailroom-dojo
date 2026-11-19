# Architecture decision records

Each record (ADR) explains one decision behind the `mailroom_record` package (project 1 of
the lean rewrite) or, in 0007, the shared leaf packages. It states the context, the options
weighed, the decision and its consequences.

The decisions come from the design, `docs/specs/2026-11-18-lean-rewrite-design.md`. The
build plan is `docs/plans/2026-11-18-record-and-store-plan.md`.

## Records

| ADR | Decision | Status |
|:---|:---|:---|
| [0001](0001-event-log-is-the-only-truth.md) | The append-only event log is the only source of truth; the index and column files are derived | Accepted |
| [0002](0002-per-document-hash-chains-and-chained-seals.md) | One hash chain per document, committed by chained seals | Accepted |
| [0003](0003-content-addressed-blobs-with-tombstones.md) | Content lives in compressed, content-addressed blobs; deletion leaves a tombstone | Accepted |
| [0004](0004-storage-formats.md) | JSONL segments per writer, Parquet at seal, SQLite (WAL) index, DuckDB for analysis | Accepted |
| [0005](0005-schema-versions-and-read-time-upgrades.md) | Every stored object carries `schema_version`; old data is upgraded on read, never rewritten | Accepted |
| [0006](0006-importing-sandbox-runs.md) | Sandbox run folders import as records; absent fields stay absent and labels stay out | Accepted |
| [0007](0007-shared-leaf-packages.md) | Shared plumbing lives in `common` and vocabularies in `dictionaries`, two leaf packages every package may use | Accepted |

## Status values

An ADR's status is one of Proposed, Accepted, Deprecated or Superseded. A superseding ADR
names the one it replaces. The old one is marked, not deleted.

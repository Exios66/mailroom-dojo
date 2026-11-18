# ADR-0003: Content-addressed blobs with tombstones

**Status:** Accepted
**Date:** 2026-11-18
**Deciders:** JJB (design approval)

## Context

Storage bloats today because the same document text is copied into every run (each v1 run
folder carries ~2.7 MB of `dataset.jsonl` for 25 documents). A client may require a document
to be deleted, but an append-only chain cannot drop entries. The repo is public, so document
text must never reach an exported analysis file.

## Decision

- Anything with content (original, transcript, window, extraction, verdict, archived record,
  human correction, report) is a blob stored once under its SHA-256, compressed with zstd
  (level 9), at `blobs/<aa>/<bb>/<sha256>.zst`. Writes go to a temp file, are fsynced, then
  renamed, so a blob is either whole or absent. The rename is durable before `put` returns
  (and so before any event names the blob): the shard directory is fsynced after it, and
  every directory on the path up to the store root is fsynced too, whether this call
  created it or found it, since a writer that crashed after `mkdir` may have left its entry
  unsynced. A `put` of content already present fsyncs the directory again before it
  returns. `put` is idempotent.
  Blob and tombstone files are created with mode 0600 (owner only), because they hold
  private document content.
- Events hold only `BlobRef(sha256, role, size)`. `payload` holds our own tokens only, never
  document text or model free text.
- Deleting content writes `<sha256>.tombstone` (reason and time) and removes the content. The
  hash stays in the chain, so verification still passes. A blob that is simply gone is
  `missing` and fails verification, naming the event.

## Options considered

### A. Content-addressed files + tombstones (chosen)
| Dimension | Assessment |
|---|---|
| Complexity | Low |
| Cost | One file per distinct content; dedup across runs |
| Scalability | Millions of files are fine with two-level fan-out |
| Team familiarity | High |

**Pros:** dedup across runs; deletion without breaking the chain; content never in exports.
**Cons:** many small files; no encryption in this package (deferred to deploy, per design).

### B. Inline content in events
**Pros:** one file per run. **Cons:** bloat, document text in every export, deletion breaks
the chain.

### C. SQLite or object store for blobs
**Pros:** fewer files (SQLite) or remote durability (object store). **Cons:** SQLite blobs
re-couple content and index; an object store adds a service. `BlobStore` keeps one class
boundary so an object-store twin can be added later.

## Consequences

- Easier: store size, deletion requests, keeping text out of Parquet and CLI output.
- Harder: verify must check blob presence; prune must check blob sharing across runs.
- Revisit when: blobs must be encrypted at rest (deploy project) or live off-host.

## Action items

1. [ ] Task 4: `BlobStore` with `put/get/exists/tombstone/size_on_disk`, 50 MB round-trip test.
2. [ ] Task 7: tombstoned passes, missing fails.
3. [ ] Task 11: prune deletes a blob only when no other run names it.

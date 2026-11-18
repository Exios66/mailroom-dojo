# mailroom-record

The `mailroom_record` package: one append-only, hash-chained run record with a
content-addressed blob store and a rebuildable index. It imports nothing from the
other workspace packages.

## Verify

`verify_run(log, blobs, run_id) -> VerifyReport` (`mailroom_record.verify`) checks a run on
disk: every line's hash on its stored form, each document's chain (seq gaps, forks, links),
the seal chain and each seal against the log, each seal's Parquet column file row by row,
and every blob an event names (missing, tombstoned, bit-rotted, or content left next to its
tombstone). It never raises on damaged data: each failure is a `Problem` naming the document,
`seq`, segment and byte offset, or seal, and the scan carries on. Only an unknown run id
raises. Segments are streamed one line at a time; no report holds document content.

`ok` is false when any problem fails. Every kind fails except two:

- `torn_tail` never fails. Its bytes were never a whole event, so no committed event is
  lost; a lost committed event shows as `seq_gap`, `truncated_after_seal` or
  `document_missing_from_log`. On a closed run (an experiment kind with a seal; an
  experiment run is sealed once, when it ends) the detail says the tail is final, a
  recovered crash; on a run still being written it may be a write in progress.
- `columns_missing` of the latest seal does not fail: its column file is written just
  after the seal file, so a crash or a seal in progress leaves it missing until the next
  `seal_run` rebuilds it. A missing older column file fails.

A run can be verified while it is written: verify reads column files, then seals, then
segments (the reverse of write order), and re-scans once, bounded by the first scan's
heads, when a document handed between writers mid-scan fakes a `seq` gap. Stored bytes
nested deeper than the JSON parser allows, lines over 64 MiB and unlistable directories
are reported, never raised.

Problem kinds: `bad_hash`, `broken_link`, `seq_gap`, `forked_chain`, `corrupt_line`,
`invalid_event`, `missing_blob`, `corrupt_blob`, `tombstone_with_content`,
`corrupt_tombstone`, `seal_mismatch`, `seal_corrupt`, `seal_missing`,
`document_missing_from_log`, `truncated_after_seal`, `columns_missing`, `columns_mismatch`,
`corrupt_run_header`, `torn_tail`. The module
docstring of `verify.py` states exactly when each is reported.

## Seal

`seal_run(log, run_id)` (`mailroom_record.seal`) commits every flushed event not yet sealed
and exports it to `columns/events-<n>.parquet`. It reads the log at most twice (a gap that
a concurrent hand-off faked is resolved, a real one refused), re-hashes and re-links every
line it exports, and rebuilds any lost or damaged column file. A column file whose seal file
is gone is refused as a deleted seal. The module docstring of `seal.py` states the rules.

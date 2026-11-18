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

`ok` is false when any problem fails. Every kind fails except `torn_tail` on a run that is
not closed: a run is closed when its header kind is an experiment kind (not
`service_period`) and it has a seal, because an experiment run is sealed once when it ends.
On a closed run a torn tail is final damage and fails.

Problem kinds: `bad_hash`, `broken_link`, `seq_gap`, `forked_chain`, `corrupt_line`,
`invalid_event`, `missing_blob`, `corrupt_blob`, `tombstone_with_content`, `seal_mismatch`,
`seal_corrupt`, `seal_missing`, `document_missing_from_log`, `truncated_after_seal`,
`columns_missing`, `columns_mismatch`, `corrupt_run_header`, `torn_tail`. The module
docstring of `verify.py` states exactly when each is reported.

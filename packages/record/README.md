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

## Reading the log

Every reader streams: a segment is read one line at a time, never past its size when the
reader opened it, so memory is bounded by the longest line (plus a few machine words per
event for the ordered readers), never by the run. `iter_events` yields grouped by `doc_id`,
each document by `seq`, in two passes: `EventLog.scan` records where each event lives, then
`RunScan.events()` reads them in order. `scan(strict=False)` records corrupt and invalid
lines as `BadLine` (no content) instead of raising. The module docstring of `log.py` states
the ordering contract.

## Index

`Index(path)` (`mailroom_record.index`) is the derived SQLite index (WAL,
`synchronous=NORMAL`): runs, documents, chain heads, the runs a document appears in, and the
review queue. No table holds document text, payloads, decisions or causes.

- `reindex(log)` is the authority: it rebuilds everything in one transaction, so readers see
  the old index or the new one, and a crash leaves the old one (or none). It never aborts on
  a damaged log: corrupt or invalid lines, gaps, forks, broken links, unreadable seals and run
  headers become `problems`, and a document stops at its last good event.
- `apply(event)` (only after the `LogWriter.flush()` that made the event durable),
  `add_run(run)` and `apply_seal(seal)` are the incremental path. They take exactly the next
  link: a repeat is a no-op, anything out of order raises `ApplyRefused` and changes nothing.
- A missing, damaged, old-version or unfinished index file is never trusted: queries raise
  `IndexUnavailable(status)`, and `ensure(log)` rebuilds it. `drift(log)` names the tables
  that differ from a fresh rebuild.
- A document is in review when its head event has kind `review`; its next event takes it out.
- `iter_events(log, run_id, doc_id)` reads one document through the byte ranges the rebuild
  recorded, plus anything written since, and falls back to a full scan when they no longer
  match.

The module docstring of `index.py` states every rule.

## Version chain and trace

`version_chain(log, run_id, doc_id)` (`mailroom_record.versions`) lists every blob a
document's events name, in event order, inputs before outputs, each blob once at its first
use: role, SHA-256, size, node, attempt, `ts`, `seq`, whether it was first an input or an
output, and its state from `BlobStore.exists` (`present`, `tombstoned` or `missing`). A blob
whose stored bytes are corrupt still shows as `present`: `verify_run` is the integrity tool.

`trace_run(log, index, run_id)` and `trace_doc(log, index, doc_id)` (`mailroom_record.trace`)
show a run's documents (nodes visited, attempts, last decision and cause, in review or not)
and every run that touched a document (steps and version chain per run). `trace_doc` reads
each document through the index (`Index.ensure`, then `Index.iter_events` and
`runs_for_document`) and falls back to the log when there is no usable index; a run or event
the index has not seen yet is still found. A trace never shows blob content, payloads, or
the values inside a decision or cause: those appear as a `Digest` (shape, field names, size,
SHA-256). Damaged data never raises: corrupt and invalid lines, gaps, forks, broken links,
torn tails and an unreadable run header become `Damage` entries, and everything readable is
still shown. Only a malformed run id (`BadRunId`) or an unknown run (`UnknownRun`) raises.
The module docstrings of `versions.py`, `trace.py` and `_reading.py` state the rules.

## Command line

`pip install -e packages/record` installs `mailroom-record` (also `python -m mailroom_record`):

```bash
mailroom-record verify <run> --root <store>          # exit 1 when the run is not ok
mailroom-record seal <run> --root <store>
mailroom-record reindex --root <store>               # rebuilds <store>/index.sqlite
mailroom-record trace run <run> --root <store>
mailroom-record trace doc <doc> --root <store>
mailroom-record versions <run> <doc> --root <store>
```

Every command takes `--json` for tools. Exit codes: 0 done (verify: ok); 1 the run is not ok,
a seal was refused, a document has no readable event but unreadable lines may hold it, or an
unexpected error; 2 a usage error (malformed run id, unknown run or document, a root that is
not a directory, a bad option); 141 the output pipe closed (`| head`), nothing printed.
Errors are one line on stderr, never a traceback. Output holds hashes, roles, node names,
kinds and counts only; control, format and separator characters in stored ids are escaped
(`\x1b`, `\u202e`) in text output.

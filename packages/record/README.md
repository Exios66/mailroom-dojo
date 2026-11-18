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

`ok` is false when any problem fails. Every kind fails except three:

- `torn_tail` never fails. Its bytes were never a whole event, so no committed event is
  lost; a lost sealed event shows as `seq_gap`, `truncated_after_seal` or
  `document_missing_from_log`. On a closed run (an experiment kind with a seal; an
  experiment run is sealed once, when it ends) the detail says the tail is final, a
  recovered crash; on a run still being written it may be a write in progress.
- `columns_missing` of the latest seal does not fail: its column file is written just
  after the seal file, so a crash or a seal in progress leaves it missing until the next
  `seal_run` rebuilds it. On a closed run, where no later seal comes, the detail says to
  run `seal_run` once more (it rebuilds the file and commits nothing new). A missing older
  column file fails.
- `unsealed_events` never fails. On a closed run it notes, per document, the events past
  the last seal (the first `seq` and how many): no seal commits them, so losing them
  would go undetected until `seal_run` commits them.

A run can be verified while it is written: verify reads column files, then seals, then
segments (the reverse of write order), and re-scans once, bounded by the first scan's
heads, when a document handed between writers mid-scan fakes a `seq` gap. Stored bytes
nested deeper than the JSON parser allows, lines over 64 MiB and unlistable directories
are reported, never raised.

Problem kinds: `bad_hash`, `broken_link`, `seq_gap`, `forked_chain`, `corrupt_line`,
`invalid_event`, `missing_blob`, `corrupt_blob`, `tombstone_with_content`,
`corrupt_tombstone`, `seal_mismatch`, `seal_corrupt`, `seal_missing`,
`document_missing_from_log`, `truncated_after_seal`, `columns_missing`, `columns_mismatch`,
`corrupt_run_header`, `torn_tail`, `unsealed_events`. The module
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

## Importing sandbox runs

`import_sandbox_run(folder, log, blobs) -> ImportResult` (`mailroom_record.importer.sandbox_run`)
turns one sandbox run folder (`dataset.jsonl`, `nodes.jsonl`, `items.jsonl`, `events.jsonl`,
`spec.lock.json`, `prompt.lock.json`, `checkpoint.json`; only `dataset.jsonl` is required)
into one sealed run with `origin="imported_sandbox"`. A document's id is its
`content_sha256` and its text becomes an `original` blob; labels (`expected_*`) are not
imported, except `expected_doc_class` as `draw_class`. Each finished node row is one event
(`review`, `error`, `decision` or `handoff`, in that order of rules) carrying its start time,
step, stage and status for the floor replay; each item row is one `output` (or `error`)
event with `extraction` and `report` blobs; `events.jsonl` and `checkpoint.json` become
`_run` events. Fields the folder does not capture are `None` and listed in `absent_fields`.
Importing a folder again is a no-op (`already_imported`); an import that died part way is
finished by the next one; a different folder under the same run id raises `ImportConflict`.

`audit_sandbox_run(folder) -> RoutingAudit` (`mailroom_record.importer.audit`) reports which
routing inputs the `legacy` router replay needs are present or absent in a folder. The module
docstrings state every mapping rule.

## Floor replay

`replay_timeline(log, run_id) -> (timeline, meta)` (`mailroom_record.replay`) rebuilds the
floor replay of any stored or imported run from the record alone, under the contract of the
sandbox's `tui/floor_replay.py:build_timeline`: a sorted list of `(at_seconds, event)` with
`{"node": ...}`, `{"item": ...}` and `{"checkpoint": ...}` events, and a `meta` dict. A node
start is played at its `started_ts`, its finish at the event's `ts`; a node in flight when
the run stopped replays as `running`. The timeline holds no document text and no extracted
value. `tests/test_replay.py` checks it against the sandbox's own `build_timeline` when
`MAILROOM_SANDBOX_CHECKOUT` names a sandbox checkout, and skips that check otherwise.

## Pack, unpack and prune

`pack_run(root, run_id, out, *, include_blobs=True)` (`mailroom_record.pack`) writes a
sealed run, and the blobs it names, to one `.tar.zst` archive with a `manifest.json` of
every member's SHA-256 and the latest seal's hash. It refuses a run whose latest seal does
not cover every flushed event (`RunNotSealed`: seal it first) or that does not verify
(`PackRefused`), never overwrites `out`, and records the pack in `runs/<run>/pack.json`.
`unpack_run(archive, root)` checks every member against the manifest and runs `verify_run`
in a staging store before anything reaches `root`; a damaged, truncated or hostile archive
is `PackCorrupt`, and a different run already in place is `UnpackConflict`. A second unpack
is a no-op; an interrupted one is finished by the next. `prune_run(root, run_id, *,
keep_columns=True)` then removes the run's segments and seals (and Parquet files with
`keep_columns=False`) and every blob of its archive that no other run names, read from the
other runs' logs. It needs a current pack, holds the store's maintenance lock exclusive
(every live writer holds it shared, so it waits for them; `StoreBusy` after
`lock_timeout`), and is resumable. Unpacking the archive restores a pruned run in place.

## Command line

`pip install -e packages/record` installs `mailroom-record` (also `python -m mailroom_record`):

```bash
mailroom-record verify <run> --root <store>          # exit 1 when the run is not ok
mailroom-record seal <run> --root <store>
mailroom-record reindex --root <store>               # rebuilds <store>/index.sqlite
mailroom-record trace run <run> --root <store>
mailroom-record trace doc <doc> --root <store>
mailroom-record versions <run> <doc> --root <store>
mailroom-record import sandbox-run <folder> --root <store>   # creates <store> when missing
mailroom-record audit sandbox-run <folder>                   # reads the folder only
mailroom-record pack <run> <archive> --root <store>  # --no-blobs leaves the blobs out
mailroom-record unpack <archive> --root <store>      # creates <store> when missing
mailroom-record prune <run> --root <store>           # --drop-columns removes Parquet files too
```

Every command takes `--json` for tools. Exit codes: 0 done (verify: ok); 1 the run is not ok,
a seal, pack, unpack or prune was refused, a document has no readable event but unreadable lines may hold it, or an
unexpected error; 2 a usage error (malformed run id, unknown run or document, a root that is
not a directory, a bad option); 141 the output pipe closed (`| head`), nothing printed.
Errors are one line on stderr, never a traceback. Output holds hashes, roles, node names,
kinds and counts only; control, format and separator characters in stored ids are escaped
(`\x1b`, `\u202e`) in text output.

# mailroom-record

The `mailroom_record` package: one append-only, hash-chained run record with a
content-addressed blob store and a rebuildable index. It imports nothing from the
other workspace packages.

The public surface is `mailroom_record.__all__`, 19 names: `Run`, `Event`, `Seal`,
`BlobRef`, `BlobStore`, `EventLog`, `Index`, `seal_run`, `verify_run`, `version_chain`,
`trace_run`, `trace_doc`, `import_sandbox_run`, `audit_sandbox_run`, `replay_timeline`,
`pack_run`, `unpack_run`, `prune_run` and `GENESIS` (`tests/test_public_surface.py`).
Everything else is reached through its module.

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

A pruned run (`runs/<run>/prune.json`: `prune_run` removed its events after packing them) is
its own state, neither ok nor damaged: `VerifyReport.pruned_archive` names the archive and
`pruned_seal` the last seal it holds, `ok` is false and `problems` is empty, because there is
nothing in this store to check. The command line prints a line starting `PRUNED` (the run,
the archive, the seal) and exits 0. Unpack the archive to verify the events themselves.

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
- A pruned run is not indexed as an empty run: `reindex` records it in a `pruned_runs` table
  (run and archive) and lists it in `IndexStats.pruned_runs`, and indexes every other run as
  usual. `Index.pruned_runs()` returns them; `documents` and `review_queue` of a pruned run,
  and `iter_events` of a pruned run (checked on the log, so a stale index cannot hide it),
  raise `RunPruned` naming the archive. Unpack the archive, then reindex.

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
still shown. Only a malformed run id (`BadRunId`) or an unknown run (`UnknownRun`) raises, and
a pruned run: `trace_run`, `version_chain`, `replay_timeline`, `EventLog.scan`, `iter_events`
and `iter_located` raise `RunPruned` naming the archive, since its events are not in this
store. `trace_doc` skips pruned runs and names them in `DocTrace.pruned_runs`.
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
`_run` events. Fields the folder does not capture are `None` and listed in `absent_fields`;
a torn last line the import skipped is in `warnings` and kept as the run's `import` event.
Importing a folder again is a no-op (`already_imported`); an import that died part way is
finished by the next one; a different folder under the same run id raises `ImportConflict`,
and a pruned run raises `RunPruned` (unpack its archive first). Importing a run that is still
being written, with a torn last line, fixes that run's history in the store without that
line: once the line is complete, the same folder conflicts (`ImportConflict`) in that store,
so import a run after it has stopped.

`audit_sandbox_run(folder) -> RoutingAudit` (`mailroom_record.importer.audit`) reports which
routing inputs the `legacy` router replay needs are present or absent in a folder. The module
docstrings state every mapping rule.

## Floor replay

`replay_timeline(log, run_id) -> (timeline, meta)` (`mailroom_record.replay`) rebuilds the
floor replay of any stored or imported run from the record alone, under the contract of the
sandbox's `tui/floor_replay.py:build_timeline`: a sorted list of `(at_seconds, event)` with
`{"node": ...}`, `{"item": ...}` and `{"checkpoint": ...}` events, and a `meta` dict. A node
start is played at its `started_ts`, its finish at the event's `ts`; a node in flight when
the run stopped replays as `running`. A naive `started_ts` is read as UTC (as the sandbox
reads it), never in the process's local zone; one that does not parse makes the start
untimed (counted in `meta["warnings"]`), never an error. The timeline holds no document text and no extracted
value. `tests/test_replay.py` checks it against the sandbox's own `build_timeline` when
`MAILROOM_SANDBOX_CHECKOUT` names a sandbox checkout, and skips that check otherwise.

Where the record's rows differ from the sandbox's, field by field, and why:

- Node rows carry a fixed list of fields: `doc`, `node`, `status`, `agent`, `model`, `step`,
  and on a finished row `stage`, `ms` and `decisions` (`None` or `[]` when the source row
  had none). The sandbox copies each source row whole, minus `ts`, so a field outside that
  list is in its row only; in the sandbox's runs that is a node's `error`, free text.
  The record never stores free text: the error's type name is the event's `cause`, not shown
  on the floor.
- A finished node row's `step`: the sandbox writes `step` on `running` rows only, so its
  finished rows have none unless the source row named one. The record shows the finished
  row's own step, else its start's, because the importer keeps the start's step on the
  finishing event (`payload.step`) to pair the two.
- `decisions` lose each `options` entry: model output text, which the importer does not
  store.
- A node `status` that was free text is stored as its SHA-256 and length, so it cannot be
  restored and replays as `None`; a number or a bool replays as stored.
- Item rows: the record shows `doc` (the item id), `error` as the cause token (the type name
  before the first `:`, else `error`), never the message, and the outcome fields
  (`doc_type`, `stage`, both confidences, `review_causes`, `review_verdict`,
  `escalation_reason`) at the top level, read from the sandbox's `pred`. The sandbox's row
  carries `pred` itself (with the extracted values), the legacy `score` and
  `usage_by_agent`; the record's does not: no extracted value is on the floor, the Dojo
  scores runs, and `usage_by_agent` stays in the item event's payload.
- `meta["warnings"]`: the sandbox's "skipped N malformed node rows" and "skipped N malformed
  item rows" are rebuilt from the import's `skipped_torn_lines`, in the sandbox's order.
  They agree for any folder the importer accepts, since it refuses a malformed line anywhere
  but at the end of a file. The record then adds "N rows had no source time" when rows were
  untimed, which the sandbox does not report. One edge: the sandbox splits lines with
  `str.splitlines` (also at `\r`, `\x0b`, `\x0c`, `\x1c` to `\x1e`, `\x85`, U+2028 and
  U+2029) and the importer at `\n` only, so a row holding such a raw character inside a
  string is one row to the record and broken rows to the sandbox.
- Times: agree within 1 ms (the record rounds to 0.1 ms). A `started_ts` the record cannot
  read as an ISO string (a native writer's value; the importer always writes ISO) is
  untimed, where the sandbox would also read a number as epoch seconds.

Scope of each comparator: the stage 1 gate's parity check compares the event sequence (node
rows by `doc`, `node` and `status`, items by id, checkpoints) and the times within 1 ms, not
the other fields. `tests/test_replay.py::test_field_parity_with_the_sandbox_build_timeline`
compares every field the record carries, field by field, and the `meta` dict, and fails on
any difference not listed here; it runs when `MAILROOM_SANDBOX_CHECKOUT` names a sandbox
checkout.

## Pack, unpack and prune

`pack_run(root, run_id, out, *, include_blobs=True)` (`mailroom_record.pack`) writes a
sealed run, and the blobs it names, to one `.tar.zst` archive with a `manifest.json` of
every member's SHA-256 and the latest seal's hash. It refuses a run whose latest seal does
not cover every flushed event (`RunNotSealed`: seal it first) or that does not verify
(`PackRefused`), never overwrites `out`, and records the pack in `runs/<run>/pack.json`.
`unpack_run(archive, root, *, lock_timeout=60.0)` checks every member against the manifest and runs `verify_run`
in a staging store before anything reaches `root`; a damaged, truncated or hostile archive
is `PackCorrupt`, and a different run already in place is `UnpackConflict`. Each member has
a size cap, read from its tar header before a byte of it is written (`MAX_RUN_FILE`, 4 GiB,
for a segment or column file; `MAX_SMALL_FILE`, 256 MiB, for `run.json`, a seal or the
manifest; `MAX_BLOB`, 4 GiB, for a blob; `MAX_TOMBSTONE`, 64 KiB), and `pack_run` refuses to
write a larger one (`PackRefused`). The archive is decompressed a few zstd blocks at a time
(at most 4 MiB per step), so a decompression bomb costs time, not memory. Only blobs the
run's events name reach `root`, each checked against its name (a `.zst` must hash to it, a
tombstone must be that hash's record); any other blob member is `PackCorrupt`. A blob the
target already holds is never replaced, so a damaged one there is `UnpackVerifyFailed` before
anything is placed, and after placing the run is verified in `root` (`UnpackVerifyFailed`
naming the problems if it fails): `unpack` prints "it verifies" only then. A second unpack
is a no-op; an interrupted one is finished by the next. While it places files it holds the
store's maintenance lock shared, as a writer does, so a prune cannot delete a blob it relies on. `prune_run(root, run_id, *,
keep_columns=True)` then removes the run's segments and seals (and Parquet files with
`keep_columns=False`) and every blob of its archive that no other run names, read from the
other runs' logs. It needs a current pack, holds the store's maintenance lock exclusive
(every live writer holds it shared, so it waits for them; `StoreBusy` after
`lock_timeout`), and is resumable. While a run is pruned (`runs/<run>/prune.json`), writers
and `seal_run` refuse it with `RunPruned` (`mailroom_record.log.refuse_if_pruned`, checked under
the maintenance lock), and so does `pack_run` (`RunPruned` naming the archive); `pack_run`
holds that lock shared from its first check until it writes `pack.json`, so a prune waits for
a pack in progress. Unpacking the recorded archive (the one `pack.json` names, or the one
`prune.json` names: the archive the run was pruned from) restores a pruned run in place; any
other archive of that run, such as an older pack, is `UnpackConflict` in this store and
changes nothing.

Recovering a damaged pack or prune record. A `pack.json` that is unreadable or not the record
`pack_run` writes, or a `prune.json` that is unreadable or names another archive, makes
`prune_run` refuse by name (`PruneRefused`) and changes nothing; a damaged `prune.json` still
counts as pruned, so writers, `seal_run` and `pack_run` keep refusing the run. To restore the
run, unpack the archive it was pruned from: `unpack` accepts the archive `pack.json` names or
the one `prune.json` names, and removes `prune.json` once the run verifies in the store. If
neither record is readable, move `runs/<run>` out of the store (keep it), then unpack the
archive the run was packed to: with no record there it is checked like any new run. Never
edit these records by hand.

## Command line

`pip install -e packages/record` installs `mailroom-record` (also `python -m mailroom_record`):

```bash
mailroom-record verify <run> --root <store>          # exit 1 when the run is not ok; PRUNED, exit 0
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

Every command takes `--json` for tools. Exit codes: 0 done (verify: ok, or pruned: a `PRUNED`
line naming the archive); 1 the run is not ok, reading a pruned run's content (`trace run`,
`versions`),
a seal, pack, unpack or prune was refused, a document has no readable event but unreadable lines may hold it, or an
unexpected error; 2 a usage error (malformed run id, unknown run or document, a root that is
not a directory, a bad option); 141 the output pipe closed (`| head`), nothing printed.
Errors are one line on stderr, never a traceback. Output holds hashes, roles, node names,
kinds and counts only; control, format and separator characters in stored ids are escaped
(`\x1b`, `\u202e`) in text output.

## Stage 1 gate

```bash
python packages/record/scripts/stage1_gate.py --runs <folder of sandbox run folders> --root <store> \
    [--sandbox <sandbox checkout>] [--report <file>] [--expect-runs <n>]
```

Imports every run folder under `--runs` into `--root`, audits and verifies each, rebuilds
the index, and writes the report (default `docs/stage1-gate-report.md`; never edit it by
hand): one row per run, source and store sizes, `original` blobs against document-runs, the
absent routing inputs, a trace check, a leak scan of the report and every Parquet file
(with a positive control), floor replay parity against the sandbox's `build_timeline` when
`--sandbox` is given, each run's import warnings, and append throughput. Exit 0 when every
condition holds, 1 otherwise. A second run with the same `--root` must find every run it
stored before `already_imported`; one that imports as `imported` again fails the gate.
`--runs` must hold at least one run folder, and `--expect-runs N` fails it unless it holds
exactly `N`. The gate speaks for the runs under `--runs` alone: a store that already holds a
run no folder there imports fails it, so give each `--runs` folder a store of its own.
`tests/test_stage1_gate.py` runs it on synthetic folders.

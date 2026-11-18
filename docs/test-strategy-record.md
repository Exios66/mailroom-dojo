# Test strategy: `mailroom_record` (project 1)

What the record package must prove, how each claim is tested, and which plan task owns the
test. The decisions being tested are in `docs/adr/`; the tasks are in
`docs/plans/2026-11-18-record-and-store-plan.md`.

## Principles

- **The record is an audit artefact.** The tests that matter most are the ones that try to
  corrupt, lose or leak it. Happy-path tests are necessary, not sufficient.
- **Tests must fail when the code is wrong.** Every tamper test asserts the exact problem
  kind, not just `ok is False`.
- **No network and no model call**, anywhere. No real document text in the repo: fixtures use
  synthetic text and a sentinel string (`SECRET-DOCUMENT-TEXT`) that leak tests search for.
- **Fast.** The whole suite runs in seconds on a laptop and in CI. Slow checks (the 15 real
  runs, throughput) live in the stage gate script, not in pytest.
- **Shared helpers** live in `packages/record/tests/record_testkit.py` (importable because the
  root `pyproject.toml` puts the tests folder on `pythonpath`); fixtures live in
  `packages/record/tests/conftest.py`.

## JJB's bar (2026-11-18)

"Tests need to be effective for testing real behavior and detect error states and points of
failure, not just simple one-off unit tests." So, for every module:

- **Failure is never silent.** For each failure the module can meet (crash, kill, torn or
  corrupt bytes, concurrent writers, disk full, short writes, fsync errors, missing or
  tampered files), a test injects it and asserts the exact error raised or problem reported.
  A failure that is swallowed, skipped without a report, or turned into a wrong answer is a
  bug, and a test must show it cannot happen.
- **Invariants, not examples.** Property-based tests (`hypothesis`) drive random sequences of
  operations (appends across writers and documents, flushes, kills, seals, tombstones) and
  check the invariants after each step: every whole line hashes to itself; per-document `seq`
  is gap-free and unique; every link holds; every event is exported to exactly one Parquet
  file; the index equals a rebuild from the log; what verify reports equals what was
  injected.
- **Real processes and real data.** Kill tests use real `SIGKILL`ed processes; concurrency
  tests use real threads and processes; the stage gate runs end to end on all stored runs.
- **Positive controls.** Every leak scan and every tamper test also proves it can see the
  thing it looks for.

### Fault injection matrix

| Fault | How injected | Modules | Expected |
|---|---|---|---|
| Process killed mid-append | `SIGKILL` a child writer | log | Whole lines verify, torn tail reported, chain continues |
| Disk full (`ENOSPC`) | Monkeypatch `os.write`/`os.fsync` to raise mid-operation | blobs, log, seal, index, pack | Clear error, writer poisoned, no partial blob or seal visible, nothing reported as success |
| Short write | `os.write` returns fewer bytes than asked | blobs, log | Retried to completion or failed loudly; never a silent partial line |
| fsync error (`EIO`) | Monkeypatch `os.fsync` | blobs, log, seal | Error raised, writer poisoned |
| Corrupt or truncated file | Flip, cut or append bytes | blobs, log, seal, pack | `BlobCorrupt`, `CorruptLine`, torn tail, `SealCorrupt`, `PackCorrupt` |
| Concurrent writers | Threads and spawned processes | blobs, log, seal | Chains intact, seals numbered without gaps, one tombstone record |
| Forked chain | Two owners of one document | log, seal, verify | `ForkedChain` refused by writers and seal, reported by verify as `forked_chain` |
| Tampering | Edit, delete, reorder lines; delete documents | verify | The exact `Problem.kind` |
| Corrupt line mid-segment | Garbage or torn bytes inserted before whole lines | verify | `corrupt_line` with segment and offset; the rest of the run is still checked |
| Bit rot | Flip, truncate or replace a blob's stored bytes | verify | `corrupt_blob` (put does not repair it) |
| Crash between tombstone and unlink | Content restored next to its tombstone | verify | `tombstone_with_content` |
| Damaged seal or column file | Edit, rehash, delete, renumber a seal; truncate, garble, edit rows of a Parquet file | verify | `seal_mismatch`, `seal_corrupt`, `seal_missing`, `columns_missing`, `columns_mismatch` |
| Damaged or old index file | Garbage, empty, truncated (half, header), flipped magic and b-tree page byte, older `schema_version`, older layout, unfinished `state` | index | Status names it; every query and `apply` raises `IndexUnavailable`; `ensure` rebuilds an equal index |
| Crash mid-rebuild | Injected `ENOSPC` from the log read, a real `SQLITE_FULL` (`max_page_count`), a `SIGKILL`ed rebuild process with and without an earlier index | index | The old index unchanged, or none (`missing`); never a half-built one |
| Damaged log under a rebuild | Corrupt and invalid lines, torn tail, gap, fork, broken link, unreadable seal and run header | index | Rebuild completes; each damage is one `problems` row with its exact kind; the document stops at its last good event |
| Damaged log under a trace | Tombstoned and missing blobs, a forked chain, a corrupt line between other lines and in place of an event, an invalid event, a torn tail, a deleted line, a broken link, a garbled run header; a valid line nested 600 deep; a segment read failing mid-scan; an index read that yields then raises | trace, versions, CLI | Each shown as its exact `Damage` kind (or blob state) with the rest of the run intact; nothing raised; trace and versions exit 0, verify exits 1 exactly when a problem fails; a half-read document is read again from the log |

## The pyramid for this package

| Layer | What | Where | Count (approx.) |
|---|---|---|---|
| Unit | Hashing, models, upgrades, blob store, single-writer log | `test_hashing.py`, `test_models.py`, `test_blobs.py`, `test_log.py` | Most tests |
| Integration | Log + seal + verify + index + trace + CLI on a temp store; importer on fixture folders; pack round trips | `test_seal.py` … `test_pack.py`, `test_importer.py` | Some |
| Fault injection | Torn tails, killed writer process, tampered lines, missing and tombstoned blobs, corrupt archives | Inside the module tests | One per failure kind |
| Stage gate (end to end) | Import, verify, trace, re-import and leak-scan all stored v1/v2 runs | `scripts/stage1_gate.py` against `/mnt/project-files/runs/autoresearch` | One run, report committed |

## What is tested, by risk

| Risk | Test type | Example test | Task |
|---|---|---|---|
| Hash changes with key order or float formatting | Unit, known vectors | `test_known_vector`, `test_key_order_does_not_change_hash` | 2 |
| Event hash misses a field | Unit | Change each field in turn; hash must change | 3 |
| Old stored event cannot be read | Unit | v0 event upgrades; newer version refused | 3 |
| Old event breaks verification after upgrade | Integration | Verify a run holding a v0 line: hash is checked on the stored form | 7 |
| Blob corrupted, half-written or duplicated | Unit | 50 MB arbitrary bytes round trip; same bytes stored once; temp file never left behind | 4 |
| Event written before its blob | Unit | `BlobNotStored` | 5 |
| Crash mid-line | Fault injection | Torn tail reported, skipped, chain continues from last whole event | 5 |
| Writer process killed mid-append | Fault injection | Child process appends in a loop, parent sends SIGKILL; reopen, every whole line verifies and the chain continues | 5 |
| Parallel writers interleave or break chains | Concurrency | 8 threads, and separately 4 processes, each with their own writer and documents | 5 |
| Edited, deleted or cut events go unnoticed | Fault injection | One test per `Problem.kind` (`test_every_problem_kind_is_produced_by_some_test`); each also asserts untouched documents still verify | 7 |
| A seal commits over damage it did not check | Fault injection | Tamper an unsealed line (stale hash, or a recomputed hash that breaks the next link), append, seal: `seal_run` refuses naming the document and `seq`, and verify reports `bad_hash` / `broken_link` | 6, 7 |
| A busy log is refused for a race | Concurrency | Four processes hand their documents between writers on every event while the sealer reads with 20 ms between segments: no seal is refused, a re-read happened, and a real gap is still refused | 6 |
| Verify crashes or stops on damaged data | Property | `test_verify_never_raises_on_any_damage`: random delete, truncate, flip, garbage on any stored file | 7 |
| A flipped byte goes unnoticed | Property | `test_clean_histories_verify_and_any_flipped_byte_is_caught`: random append/seal histories verify clean; any one-byte flip in a segment or seal fails, or lands in a reported torn tail | 7 |
| A recovered crash fails a finished run forever | Fault injection | Crash mid-append (injected short write + ENOSPC, and a real SIGKILL), reopen, append, seal: the torn tail is reported (final on a closed run) and never fails; lost committed events still fail as `seq_gap` / `truncated_after_seal` | 7 |
| Verify falsely fails a run being written | Concurrency | A seal made, and a hand-off done, after verify read part of the log; a seal without its column file yet; four hand-off processes plus a sealing process while verify scans slowly: every report is ok, and a real gap is still reported | 7 |
| Hostile stored bytes crash verify | Fault injection, property | JSON nested 200,000 deep in a segment, seal, run header or tombstone, and a valid line whose payload nests 5,000 deep: reported, never raised (seal_run raises `CorruptLine`); the never-raises property also draws deep nesting; a 24 MB line is reported under a lowered limit with a bounded peak | 7 |
| A broken tombstone passes as a deletion | Fault injection | Empty, directory, not JSON, other hash, no reason: `corrupt_tombstone` | 7 |
| Verify holds a large run in memory | Integration | 24 MB of payloads: traced peak stays under 6 MB; one line with seq 10^9 and one seal numbered 10^9 are each reported in under 5 s (asserted) | 7 |
| Whole document deleted between seals | Fault injection | Seal lists documents; deleting a sealed document gives `document_missing_from_log` (a document added and deleted between two seals is not detectable: no seal names it) | 7 |
| Tombstone confused with loss | Unit | Tombstone passes, missing blob fails and names the event | 7 |
| Index drifts from the log | Integration, equivalence | Incremental index equals full reindex; deleting the index and rebuilding gives the same dump | 8 |
| Index drifts under real histories | Property | `test_incremental_equals_reindex_over_random_histories`: two runs sharing documents, three writers each, handoffs, flushes, seals and repeated applies; after every step the index equals a rebuild of the log (`drift == []`) | 8 |
| An apply out of order corrupts head state | Unit | Gap, fork and broken link each raise `ApplyRefused` and change nothing; repeats and events a rebuild covered are no-ops | 8 |
| A reader sees a half-built index | Concurrency | A reader process dumps in a loop while a slowed rebuild runs: only the old or the new digest is ever seen, and the old one mid-build | 8 |
| Applies race a rebuild | Concurrency | Four writer threads apply after each flush while a thread rebuilds repeatedly; the end state equals a rebuild | 8 |
| A per-document read scans the whole run | Integration, property | Reads touch one writer's lines; tails written after the rebuild and new segments are read; a shrunk segment falls back; any layout equals the log's read | 8 |
| A reader holds a large run in memory | Integration | 26 MB run: every log reader, the rebuild and per-document reads peak under 6 MB (the old readers peaked at 28-54 MB) | 8 |
| A reader glues a half-written line | Fault injection | A line finished while a reader iterates is never returned as a line of its own | 8 |
| Version chain misses or repeats a version | Integration | Original, transcript, two extraction attempts, archived record, review, human correction: six versions in order, each blob once at its first use, inputs before outputs, states from the blob store | 9 |
| Trace hides a run or reads a stale index | Integration | `trace doc` lists every run in `created_at` order; a run and events written after the index was built are found; reads go through `Index.iter_events` (no whole-run scan, nothing of a run without the document); no usable index falls back to the log; a run the index lists but the log lost is reported | 9 |
| CLI exit codes drift or print a traceback | Integration | Exit 0/1/2 for every command and error class (malformed and unknown run ids, unknown document, root missing or a file, missing option, refused seal, unexpected exception), one line on stderr; the installed console script and `python -m mailroom_record` run as subprocesses | 9 |
| CLI echoes stored content | Property, leak scan | `test_cli_output_never_holds_stored_content`: random histories with the sentinel in blobs, payload keys and values, decisions, causes, tombstone reasons, corrupt and invalid lines; every command in text and JSON prints no sentinel (positive control: the sentinel is in the segments; a mutation that echoes a cause is caught) | 9 |
| Document text leaks into an export | Leak scan | Sentinel string absent from Parquet, SQLite (every table, and the raw file and WAL bytes, with the sentinel in cause, decision, payload and an invalid line), CLI output, pack manifest, gate report | 6, 8, 9, 11, 12 |
| Payload carries free text | Unit | Parquet has no `payload` column; importer drops decision `options` text | 6, 10 |
| Re-import duplicates a run or blobs | Idempotency | Second import is `already_imported`, blob bytes on disk unchanged | 10 |
| Importer guesses a missing field | Unit | Missing optional files listed in `absent_fields`, value is `None` | 10 |
| Labels leak into the record | Unit | No `expected_fields` anywhere in imported events | 10 |
| Sandbox format drifts | Contract | Fixture folders mirror the real file shapes; the stage gate re-proves on real folders | 10, 12 |
| A pack is corrupt or incomplete | Fault injection | Flipped byte gives `PackCorrupt`; unsealed run refused | 11 |
| Prune deletes a shared blob | Integration | Two runs share a document; prune one; the other verifies | 11 |
| Public surface drifts | Contract | `__all__` equals the 18 names | 12 |
| Boundary creep | Static | AST walk: no import of sibling workspace packages | 1 |

## Additions beyond the plan's sketches

(Items 6 and 7 added 2026-11-18 after JJB's bar above.)

These come from the ADRs and are folded into the task briefs:

1. **Process-level concurrency** (Task 5). Review Focus 4 says "threads or processes"; the
   plan's sketch only uses threads. Add a `multiprocessing` test with separate writers.
2. **Kill test** (Task 5). A real `SIGKILL` of a writer process, not only a hand-written torn
   line.
3. **Verify an upgraded event** (Task 7, ADR-0005). Verification hashes the stored form.
4. **Leak scans with a sentinel** (Tasks 6, 8, 9, 11, 12), not only for the CLI.
5. **Throughput figure** (Task 12). The gate report records append throughput with
   `flush_every=16` (events per second, documents per minute at about 20 events per
   document) against the design's 249 documents a minute floor. It is reported, not asserted
   in pytest, so CI does not flake on slow runners.
6. **Property-based invariant tests** with `hypothesis` (added to the `dev` extra) for
   hashing, blobs, the log and seals now, and for verify, the index and pack as they land.
7. **The fault injection matrix above**, applied to every module; disk-full, short-write and
   fsync-error cases for blobs, the log and seals are added before Task 7 starts.

## Coverage target

No coverage tool is configured, and none is added in project 1. The target is behavioural:
every public function in `__all__` has at least one test, every `Problem.kind` has a test
that produces it, and every Review Focus item in the plan has a named test. Each task's
independent review checks this list for its module.

## Gaps left for later projects

- Postgres index twin, blob encryption at rest and the periodic seal timer: tested in the
  engine and deploy projects.
- Resume semantics (skip a step whose result is recorded): engine project; this package only
  guarantees that the recorded result is durable and findable.
- Multi-host writers: out of scope (ADR-0004).

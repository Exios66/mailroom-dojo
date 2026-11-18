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
| Edited, deleted or cut events go unnoticed | Fault injection | One test per `Problem.kind` | 7 |
| Whole document deleted between seals | Fault injection | Seal lists documents; deletion gives `document_missing_from_log` | 7 |
| Tombstone confused with loss | Unit | Tombstone passes, missing blob fails and names the event | 7 |
| Index drifts from the log | Integration, equivalence | Incremental index equals full reindex; deleting the index and rebuilding gives the same dump | 8 |
| Document text leaks into an export | Leak scan | Sentinel string absent from Parquet, SQLite, CLI output, pack manifest, gate report | 6, 8, 9, 11, 12 |
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

# Test strategy: `mailroom_desk` (project 7)

What the review desk must prove, how each claim is tested, and which plan task owns the
test. The design is `docs/adr/0007-review-desk-is-event-sourced-on-the-record.md`; the
tasks are in `docs/plans/2026-11-18-review-desk-plan.md`. The bar is the record's
(`docs/test-strategy-record.md`, "JJB's bar"): real behaviour and failure states, not
one-off unit tests.

## Principles

- **The record is the desk's only state**, so tests build real record roots with
  `mailroom_record`'s own APIs (`EventLog.create_run`, `open_writer`, `BlobStore.put`,
  `seal_run`, `Index`) under `tmp_path` and read the result back from disk. Nothing in the
  record is mocked.
- **Every refusal is asserted by its exact error** and by what did not happen: the segment
  bytes are unchanged, no review event exists, the lock is free afterwards.
- **Real processes.** Concurrency and crash tests spawn real interpreters
  (`multiprocessing` `spawn`) and `SIGKILL` them. A desk clock that sleeps (read under the
  lock, between the in-review check and the append) widens the race window on every run,
  so the race tests do not depend on a lucky schedule.
- **Invariants, not examples.** A `hypothesis` property drives random histories and
  checks the fold against the record index after every step.
- **Hermetic and fast.** No network, no model call, no shared paths (CI may run several
  xdist workers); synthetic tokens, classes and reviewers only. The suite runs in about
  ten seconds with `-n 4`.
- **Shared helpers** live in `packages/desk/tests/desk_testkit.py` (on `pythonpath`, so
  spawned children can import the process targets); fixtures live in `conftest.py`.

## Fault and refusal matrix (D1)

| Fault or refusal | How injected | Expected | Test |
|---|---|---|---|
| Two processes enqueue one document | Two spawned children behind a barrier, three documents, slow clock | Exactly one `enqueued` and one `AlreadyInReview` per document; one review event each; `verify_run` ok | `test_processes.py::test_two_processes_enqueueing_one_document_enqueue_it_once` |
| Processes enqueue different documents | Four children, five documents each | All enqueued; queue holds all twenty; `verify_run` ok | `test_processes_enqueueing_different_documents_all_succeed` |
| Desk killed holding the lock | Child's clock writes a marker and hangs inside `enqueue`; parent checks the lock is held, then `SIGKILL` | Lock free, no event, `verify_run` ok, next enqueue proceeds | `test_desk_killed_between_lock_and_append_leaves_a_clean_record_and_no_lock` |
| Lock held too long | A child process (and, separately, this process) holds the desk lock | `DeskLockTimeout` after the timeout, nothing appended; freed when the holder dies | `test_lock_held_by_another_process_times_out_then_frees_on_its_death`, `test_lock_timeout_refuses_and_appends_nothing` |
| Threads of one process | Six threads, one document | One enqueue, five `AlreadyInReview` | `test_threads_of_one_process_are_serialised` |
| Torn tail | Half a line appended to the desk's segment, and to another writer's | Skipped, not an event; the desk writes a new segment; `verify_run` ok with one non-failing `torn_tail` | `test_torn_tail_in_the_desk_segment_is_skipped_and_writing_continues`, `test_torn_tail_in_another_writers_segment_does_not_block_the_desk` |
| Corrupt line, unreadable header, forked chain | Garbage line; broken `run.json`; a seq-0 line copied into a second segment | `RecordDamaged`, nothing appended, lock free | `test_corrupt_line_is_refused_as_damage`, `test_unreadable_run_header_is_damage`, `test_forked_chain_is_refused_as_damage` |
| Missing or tombstoned shown blob | A ref to a hash never stored; a tombstoned blob; a blob tombstoned under the lock | `BlobNotPresent` with the state; nothing appended | `test_missing_or_tombstoned_shown_blob_is_refused`, `test_blob_tombstoned_between_check_and_append_is_refused` |
| Sealed experiment run | `seal_run` on a `specialist_only` run | `RunClosed` (also for a sample-only request); a `service_period` run stays open | `test_sealed_experiment_run_is_refused`, `test_unsealed_experiment_and_sealed_service_runs_stay_open` |
| Seal racing the closed check | `seal_run` started while the desk is between check and append | The seal waits, then covers the review event; the next enqueue is `RunClosed` | `test_seal_cannot_land_between_the_closed_check_and_the_append`, `test_seal_guard_is_the_lock_seal_run_takes` (pins the record layout) |
| Seal landing before the desk takes its locks | The run is sealed after the first closed check, while the enqueue waits for a desk lock held by the test | The check repeated under the locks refuses with `RunClosed`; no segment changes (deleting that re-check fails this test) | `test_seal_landing_while_waiting_for_the_desk_lock_is_seen_under_the_lock` |
| Unknown run | Missing run, path-like and over-long ids | `UnknownRun`; no directory or lock file created | `test_unknown_run_is_refused` |
| Busy chain | A live writer of this process holds the document, or the `desk` writer id | `RecordBusy`, nothing appended | `test_document_held_by_a_live_writer_in_this_process_is_busy`, `test_desk_writer_id_held_elsewhere_is_busy` |
| Outage cause | `transient_exhausted`, `provider_unavailable`, as a token or a segment | `OutageNotReviewable` in requests; refused in a policy's catalog and rules | `test_outage_causes_are_refused`, `test_outage_tokens_are_refused_in_the_catalog_and_rules` |
| Index refuses the apply | A run created after the index was built | The event is durable; `index_error` names `unknown_run`; a reindex shows it | `test_index_refusal_is_reported_not_raised` |
| Free text in a payload | Sentinel text in a shown blob | The sentinel is absent from every segment; the blob hash is present (positive control) | `test_payload_holds_no_free_text` |

## What is tested, by risk

| Risk | Test type | Where |
|---|---|---|
| A rule matches wrongly or out of order | Unit, each branch (`cause_in`, `class_in`, `confidence_below` strictly below, all conditions together, patterns) and a reordered policy | `test_policy.py` routing tests |
| A policy without a default, or a hidden rule, loads | Validation | `test_missing_default_rule_is_refused`, `test_default_rule_before_the_end_is_refused` |
| Bad YAML loads silently | Validation: duplicate keys (top and nested), unhashable keys, python tags, unknown keys at every level, bad tokens, bad types (strings, bools, NaN, infinities, ranges) | `test_policy.py`, `test_reviewers.py` |
| The policy hash misses a change or changes without one | Property-like table: every rule, cause and sample field mutated in turn changes it; key order, comments and `24` vs `24.0` do not | `test_any_change_of_meaning_changes_the_hash`, `test_hash_ignores_key_order_comments_and_number_format` |
| The sample is biased or not repeatable | 10,000 ids at three rates within five binomial sigmas; same seed same set, other seed other set; rate 0 none, 1 all; monotone in the rate; strict threshold | `test_sampling_*` |
| A strict registry admits an unknown reviewer | Unit: strict and open modes, tokens, roles | `test_reviewers.py` |
| The fold disagrees with the index | Property: random histories over two runs and four documents of non-review events, foreign review events (unreadable payloads, a claim with no opening) and desk enqueues; after every step the fold, the plain desk queue and the index-backed desk queue equal `Index.review_queue()` of both an applied index and a rebuilt one, and `drift == []` | `test_fold.py::test_fold_agrees_with_the_index_on_random_histories` |
| A lease or deferral outlives its time | Injected clock at the boundary (one microsecond before; exactly at) | `test_claim_holds_until_the_lease_expires_exactly`, `test_injected_clock_drives_lease_expiry_in_the_queue`, `test_release_escalate_defer_and_reopen` |
| SLA breach is off by one | Boundary: due minus 1 us, due, due plus 1 us; a resolved case never breaches | `test_sla_breach_boundaries`, `test_resolution_ends_the_episode` |
| Queue order is wrong | Priority, then due time, then opened time, then id; an unreadable case first | `test_queue_order_priority_then_due_then_opened_then_id` |
| An unreadable case drops out of the queue | Garbage review payloads stay in the queue with `anomalies` that never echo content | `test_unreadable_and_out_of_place_payloads_are_anomalies_not_drops` |
| Boundary creep | Static AST walk: the desk imports only record and labels; record and labels import nothing of the desk; positive controls | `test_boundaries.py` |
| A class, field or cause token in code | Static: no class, field or subclass of the labels' synthetic profile, and no cause token of `config/review/default.yaml` (other than the reserved `spot_check`), is quoted in library code | `test_boundaries.py` |

## Mutation pass (D1)

Each mutation below was applied to the source alone, the desk suite was run, and the
source was restored (`git diff` empty afterwards). Every one made the suite fail; the
failing tests are listed in the D1 hand-off report. Mutated: rule order, the sampling
threshold, the in-review (idempotency) check, the outage check, lock release, taking the
desk lock, the episode boundary, seeing the resolution, the lease comparison, the policy
hash input, the SLA comparison, the closed-run check, taking the seal lock, priority
order, the `confidence_below` comparison, strict registry mode, the default-rule check,
duplicate YAML keys and the unknown-cause check.

## Later tasks

- D2: assign, claim, release, escalate, defer, reopen and resolve through the desk API,
  each refusal (`AssignedToOther`, `AlreadyResolved`, `StaleCase`) with real processes; the
  correction blob.
- D3: case view and `export-labels` round trip to a gold label with the reviewer.
- D4: the `human_review` node with a fake engine loop over a real record; the CLI.
- D5: the HTTP API, old-shape compatibility routes, token identity.

## Known gaps

- A crash between the desk's flush and the index apply leaves the index behind the log;
  `index.drift` shows it and `reindex` repairs it (the record's rule), but no test kills a
  process at that exact point.
- Multi-host writers are out of scope (ADR-0004): `flock` serialises one host only.

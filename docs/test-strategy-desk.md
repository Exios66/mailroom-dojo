# Test strategy: `mailroom_desk` (project 7)

What the review desk must prove, how each claim is tested, and which plan task owns the
test. The design is `docs/adr/0008-review-desk-is-event-sourced-on-the-record.md`; the
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
- **Linux-only tests are marked.** Two tests name a descriptor's file through
  `/proc/self/fd` (the seals-fsync spy and the full-disk injection) and carry a
  `LINUX_ONLY` skip marker, so on another platform they are reported as skipped, not
  failed. Everything else needs only POSIX (`flock`, `fork`, `SIGKILL`).

## Fault and refusal matrix (D1)

| Fault or refusal | How injected | Expected | Test |
|---|---|---|---|
| Two processes enqueue one document | Two spawned children behind a barrier, three documents, slow clock | Exactly one `enqueued` and one `AlreadyInReview` per document; one review event each; `verify_run` ok | `test_processes.py::test_two_processes_enqueueing_one_document_enqueue_it_once` |
| Processes enqueue different documents | Four children, five documents each | All enqueued; queue holds all twenty; `verify_run` ok | `test_processes_enqueueing_different_documents_all_succeed` |
| Desk killed holding the lock | Child's clock writes a marker and hangs inside `enqueue`; parent checks the lock is held, then `SIGKILL` | Lock free, no event, `verify_run` ok, next enqueue proceeds | `test_desk_killed_between_lock_and_append_leaves_a_clean_record_and_no_lock` |
| Lock held too long | A child process (and, separately, this process) holds the desk lock | `DeskLockTimeout` after the timeout, nothing appended; freed when the holder dies | `test_lock_held_by_another_process_times_out_then_frees_on_its_death`, `test_lock_timeout_refuses_and_appends_nothing` |
| Fork while holding the lock | `os.fork()` inside `desk_lock`; the child blocks on a pipe; the parent leaves the lock; positive control: a `spawn` child started under the lock | The forked child keeps the lock (`DeskLockTimeout` with timeout 0) until it exits, then the lock is free; the spawned child never holds it | `test_processes.py::test_a_child_forked_without_exec_keeps_the_lock_after_the_parent_lets_go`, `test_a_spawned_child_does_not_inherit_the_lock` |
| Threads of one process | Six threads, one document | One enqueue, five `AlreadyInReview` | `test_threads_of_one_process_are_serialised` |
| Torn tail | Half a line appended to the desk's segment, and to another writer's | Skipped, not an event; the desk writes a new segment; `verify_run` ok with one non-failing `torn_tail` | `test_torn_tail_in_the_desk_segment_is_skipped_and_writing_continues`, `test_torn_tail_in_another_writers_segment_does_not_block_the_desk` |
| Corrupt line, unreadable header, forked chain | Garbage line; broken `run.json`; a seq-0 line copied into a second segment | `RecordDamaged`, nothing appended, lock free | `test_corrupt_line_is_refused_as_damage`, `test_unreadable_run_header_is_damage`, `test_forked_chain_is_refused_as_damage` |
| Missing or tombstoned shown blob | A ref to a hash never stored; a tombstoned blob; a blob tombstoned under the lock | `BlobNotPresent` with the state; nothing appended | `test_missing_or_tombstoned_shown_blob_is_refused`, `test_blob_tombstoned_between_check_and_append_is_refused` |
| Pruned run | Two runs with a case each; one sealed, packed and pruned with the record's own API (experiment and service-period kinds) | `queue()` lists only the live run's case; `queue(run_id)`, `case` and `enqueue` (with causes and sample-only) on the pruned run raise `RunArchived`; nothing appended | `test_a_pruned_run_is_skipped_by_the_whole_queue_and_refused_by_name` |
| Sealed experiment run | `seal_run` on a `specialist_only` run | `RunClosed` (also for a sample-only request); a `service_period` run stays open | `test_sealed_experiment_run_is_refused`, `test_unsealed_experiment_and_sealed_service_runs_stay_open` |
| Run-level chain | `_run` with causes, and as a sample-only request the sample would select | `RunChainNotReviewable`; no segment changes; the same sample-only request for a document enters | `test_run_level_chain_is_refused` |
| Seal racing the closed check | `seal_run` started while the desk is between check and append | The seal waits, then covers the review event; the next enqueue is `RunClosed` | `test_seal_cannot_land_between_the_closed_check_and_the_append`, `test_seal_guard_is_the_lock_seal_run_takes` (pins the record layout without a sleep: waits for `seal_run` to reach its `flock` and compares the inode with the guard's) |
| Seal landing before the desk takes its locks | The run is sealed after the first closed check, while the enqueue waits for a desk lock held by the test | The check repeated under the locks refuses with `RunClosed`; no segment changes (deleting that re-check fails this test) | `test_seal_landing_while_waiting_for_the_desk_lock_is_seen_under_the_lock` |
| `seals/` created by the desk | `seal_guard` on a run without `seals/`, and again with it; `os.fsync` spied | `seals/` then the run directory are fsynced after the `mkdir`, on every call; a missing run directory is `FileNotFoundError` and creates nothing. Durability across a power cut itself is not testable here | `test_seal_guard_creates_the_seals_directory_durably`, `test_seal_guard_on_a_missing_run_directory_creates_nothing` |
| Unknown run | Missing run, path-like and over-long ids | `UnknownRun`; no directory or lock file created | `test_unknown_run_is_refused` |
| Busy chain | A live writer of this process holds the document, or the `desk` writer id | `RecordBusy`, nothing appended | `test_document_held_by_a_live_writer_in_this_process_is_busy`, `test_desk_writer_id_held_elsewhere_is_busy` |
| Outage cause | `transient_exhausted`, `provider_unavailable`, as a token or a segment | `OutageNotReviewable` in requests; refused in a policy's catalog and rules | `test_outage_causes_are_refused`, `test_outage_tokens_are_refused_in_the_catalog_and_rules` |
| Full disk on the flush | `os.write` raises `ENOSPC` for segment files (like the record's fault tests) | `RecordWriteFailed` with the `OSError` as `__cause__`; nothing in the segment; the lock is free; the next enqueue and `verify_run` succeed | `test_full_disk_on_flush_is_a_typed_error_and_leaves_the_record_usable` |
| Close fails after a good flush | `LogWriter.close` raises `EIO` after closing | `enqueue` returns the durable event; the lock is free; `verify_run` ok | `test_close_failing_after_a_good_flush_does_not_raise` |
| Time at the edges | A desk clock at 9999-12-31T23:00 (the SLA due time overflows); a record-valid far-future review event from another writer; an escalation at 9999-12-31T23:00 with a new SLA; a clock in year 5 | `enqueue` returns its durable event (never raises after the append); the due time saturates at `END_OF_TIME` with exactly one anomaly (on the opening and the escalate path); the queue still lists every case; a four-digit year is written | `test_enqueue_at_the_end_of_time_returns_its_event`, `test_one_far_future_event_does_not_take_down_the_queue`, `test_escalate_past_the_end_of_time_saturates_with_an_anomaly`, `test_enqueue_at_the_start_of_time_writes_a_valid_timestamp` |
| Index behind the log | A desk without the index enqueues, an engine writes a review event, both unseen by the index | `queue()` and `case()` give the same answers as a desk with no index (they read the log only); `index_drift()` reports `r1: 2 document head(s) differ from the log`; after `reindex` it is `[]`; an index that knows the document but holds an older head is drift too | `test_stale_index_changes_no_answer`, `test_index_behind_on_a_known_document_is_drift` |
| Index broken or locked | A directory in place of `index.sqlite` (a raw `sqlite3.OperationalError` the record does not translate); another connection holding `BEGIN EXCLUSIVE` with `busy_timeout=0` | `queue()` and `case()` answer from the log; `enqueue` appends durably and reports `index_error` (`OperationalError: ...`, not only `ApplyRefused`); `index_drift()` reports the index instead of raising; `verify_run` ok | `test_broken_index_changes_no_answer`, `test_locked_index_changes_no_answer` |
| Index refuses the apply | A run created after the index was built | The event is durable; `index_error` names `unknown_run`; a reindex shows it | `test_index_refusal_is_reported_not_raised` |
| `index_drift` on bad input | An unknown run, a path-like, non-string or over-long id, an index never built, a corrupt log line, a broken run header | One problem line each (the id shortened), never an exception; reading still works without the index | `test_index_drift_reports_and_never_raises` |
| Reading cost grows with the cases | A counting wrapper on the desk's `EventLog` (`scan`, `iter_stored`, `doc_head_seqs`, `iter_located`) and an index that fails on any read | `queue()` over two runs is exactly one `scan` per run, `queue("r1")` one, `case()` one scan of its document; the index is applied to (13 events) and never read. Deterministic: counts, not wall-clock | `test_reading_scans_each_run_once_and_never_reads_the_index` |
| Read error while appending | `LogWriter.append` raises `EIO` (it only reads the chain head and buffers) | `RecordDamaged` (not `RecordWriteFailed`) with the `OSError` as cause; nothing written; the lock is free | `test_read_error_while_appending_is_damage_not_a_write_failure` |
| Free text in a payload | Sentinel text in a shown blob | The sentinel is absent from every segment; the blob hash is present (positive control) | `test_payload_holds_no_free_text` |

## What is tested, by risk

| Risk | Test type | Where |
|---|---|---|
| A rule matches wrongly or out of order | Unit, each branch (`cause_in`, `class_in`, `confidence_below` strictly below, all conditions together, patterns) and a reordered policy | `test_policy.py` routing tests |
| A policy without a default, or a hidden rule, loads | Validation | `test_missing_default_rule_is_refused`, `test_default_rule_before_the_end_is_refused` |
| Bad YAML loads silently | Validation: duplicate keys (top and nested), unhashable keys, python tags, unknown keys at every level, bad tokens, bad types (strings, bools, NaN, infinities, ranges) | `test_policy.py`, `test_reviewers.py` |
| Hostile config crashes the loader | Integers too large for a float (`sla_hours`, `confidence_below`, `sample.rate`, an integer seed), integer literals too long to read, YAML and Python values nested 5,000 deep, a validator raising `RecursionError`/`ValueError`/`OverflowError`: each is a `PolicyError` or `RegistryError`, and problem messages show values shortened (a 10,000-character queue name and a 4,000-digit rate are cut to under 120 characters); an integer seed too long for text is exactly that problem | `test_integers_too_large_for_a_float_*`, `test_deeply_nested_yaml_is_a_policy_error`, `test_an_oversized_value_is_shown_shortened_in_the_problem`, `test_an_integer_seed_too_long_for_text_is_a_problem`, `test_a_validator_crash_on_hostile_input_is_still_a_policy_error`, `test_hostile_sizes_are_registry_errors` |
| The policy hash misses a change or changes without one | Property-like table: every rule, cause and sample field mutated in turn changes it; key order, comments and `24` vs `24.0` do not | `test_any_change_of_meaning_changes_the_hash`, `test_hash_ignores_key_order_comments_and_number_format` |
| The sample is biased or not repeatable | 10,000 ids at three rates within five binomial sigmas; same seed same set, other seed other set; rate 0 none, 1 all; monotone in the rate; strict threshold | `test_sampling_*` |
| A strict registry admits an unknown reviewer | Unit: strict and open modes, tokens, roles | `test_reviewers.py` |
| The fold disagrees with the index | Property: random histories over two runs, four documents and the run-level chain `_run`, of non-review events, foreign review events (also on `_run`, which the queue then shows, as the index does; unreadable payloads, assign, live and lapsed claims, defer, release, a claim with no opening) and desk enqueues; after every step the queues of a desk without and with an index are the same list (cases, statuses, order), their membership equals `Index.review_queue()` of an applied and a rebuilt index and a model the test keeps from the operations alone, each status equals the model's, the queue is in `sort_key` order, enqueue refuses exactly when the model says so (`AlreadyInReview`, `RunChainNotReviewable`), no `human_review` event follows a review event, and `drift == []` | `test_fold.py::test_fold_agrees_with_the_index_on_random_histories` |
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

### Re-check after the D1 review fixes

The same procedure on the fixed code (`git diff` empty after each). Caught, with the test
that fails: reopen keeping the assignee (`test_reopen_clears_assignment_claim_and_deferral`),
claim keeping a deferral (`test_claim_clears_a_deferral`), `authenticate` dropping the role
(`test_authenticate_finds_the_holder_of_a_token`), `sort_key` with opened before due
(`test_queue_order_priority_then_due_then_opened_then_id`), no closed-run re-check under the
lock (`test_seal_landing_while_waiting_for_the_desk_lock_is_seen_under_the_lock`), a plain
`mkdir` of `seals/` or no run-directory fsync
(`test_seal_guard_creates_the_seals_directory_durably`), no `_run` refusal
(`test_run_level_chain_is_refused` and the property), a flush `OSError` left untyped, no
SLA saturation or no saturation anomaly, a year without four digits, trusting a stale index,
raising on an index apply error, drift by membership only, a close `OSError` escaping, a
space allowed in a reference, and deep YAML not caught. One mutation is equivalent and
survives by design: treating an integer too large for a float as a number in
`policy._is_number` (the range checks that follow refuse it anyway).

### Re-check after the second D1 review

The second review's survivors, applied the same way on the final code, each now caught
(`git diff` empty after each): `show()` not shortening (M4,
`test_an_oversized_value_is_shown_shortened_in_the_problem`); `_apply` catching only
`ValueError`/`RuntimeError` (M6, the broken and locked index tests); an unreadable index
reported as no drift (M7, `test_broken_index_changes_no_answer`,
`test_index_drift_reports_and_never_raises`); the escalate path saturating without an
anomaly (M9, `test_escalate_past_the_end_of_time_saturates_with_an_anomaly`); an append
`OSError` mapped to `RecordWriteFailed` (M13,
`test_read_error_while_appending_is_damage_not_a_write_failure`); the "seed too long"
problem dropped (M15, `test_an_integer_seed_too_long_for_text_is_a_problem`);
`index_drift` skipping the run check (M19, `test_index_drift_reports_and_never_raises`).
On the log-only read path: a per-case re-scan (`test_reading_scans_each_run_once_and_never_reads_the_index`),
`queue(run_id)` ignoring its run (that test, the property and
`test_stale_index_changes_no_answer`), dropping the `_run` chain from the queue (the
property, after its foreign events were allowed on `_run`), a sort by ids instead of
`sort_key`, folding only the first document of a run, drift ignoring documents the index
lacks, `index_drift` raising on a damaged log and `case()` ignoring its `now`: all caught.

## Later tasks

- D2: assign, claim, release, escalate, defer, reopen and resolve through the desk API,
  each refusal (`AssignedToOther`, `AlreadyResolved`, `StaleCase`) with real processes; the
  correction blob.
- D3: case view and `export-labels` round trip to a gold label with the reviewer.
- D4: the `human_review` node with a fake engine loop over a real record; the CLI.
- D5: the HTTP API, old-shape compatibility routes, token identity.

## Known gaps

- A crash between the desk's flush and the index apply leaves the index behind the log.
  The desk never reads the index, so no answer changes (tested with an index that was never
  given the events); `Desk.index_drift()` names the run and `reindex` repairs it. No test
  kills a process at that exact point.
- References bound what fits in a payload but cannot stop a short label or a path-like
  string (`a/../../etc` is accepted); `prefill_ref` must be treated as an opaque name by
  D3, never as a path. Tested: whitespace, controls, non-ASCII and over-long values are
  refused.
- A process forked without `exec` while it holds a desk lock keeps the lock (tested and
  documented, not prevented): workers must be started with `spawn` or `subprocess`.
- Multi-host writers are out of scope (ADR-0004): `flock` serialises one host only.

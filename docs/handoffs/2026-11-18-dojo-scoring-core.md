# Handoff: Dojo scoring core (project 3, chunk 1)

Date: 2026-11-18. Repo: Exios66/mailroom-dojo. Branch: `claude/project-thread-fmjkqn` (pushed), built on the
run-record branch `claude/project-thread-h3rj4z`. PR: see the PR for this branch.

## What is done

Package `packages/dojo` (`mailroom_dojo`), ported from `llm-dojo-scoring` 23f8234c (0.30.1) with no
process-global state, no environment variables, no network and no import of the old package.

| Piece | Where | Proof |
|---|---|---|
| Scoring profile (YAML, immutable, hashable, validated) | `profile/`, `config/clients/mailroom-dataset/scoring.yaml` | loader tests, label-folding parity vs old Dojo |
| Value helpers, dates, money, text normalization | `values/`, `normalization/` | 21,009-case differential corpus, clock-independence property |
| Scalar scorers, scorer registry, similarity backend (optional, injected), CMS identifier plug-in | `field_scoring/` | same corpus, no-global-state and no-socket tests |
| Entity lists, audits, presence, row variants, trace capture, `score_extraction` | `field_scoring/` | 16 old golden calls tied to `scores.json`, extraction differential corpus, fault and property tests |
| Classification labels and metrics (profile-driven class set) | `classification/` | 7,774 of 7,774 classification cases identical, 674 recorded golden boundary calls identical |
| Label interface (structural Protocols) and Hub-row reference adapter | `labels_interface/` | 1,194 of 1,194 cases identical |
| New document type with config only (R4) | `tests/test_new_document_type.py` | adds two synthetic classes, no source file changes, no existing score moves |
| Package boundary test | `tests/test_boundaries.py` | AST walk: no engine, no old package, no env, no network |

Docs: `docs/plans/2026-11-18-dojo-scoring-core-plan.md`, `docs/plans/2026-11-18-dojo-scoring-port-inventory.md`,
`docs/test-strategy-dojo-core.md`, `packages/dojo/docs/score-changelog.md` (found-not-changed notes, no intended
differences), `packages/dojo/docs/golden-parity.md` (how fixtures are captured and regenerated).

## State at handoff

- Independent Opus review of the whole diff (fresh reviewer, re-ran the evidence, 12,000 own differential cases and
  4,500 own `score_extraction` cases against the old Dojo: 0 mismatches). Verdict: accept with fixes. Fixed here:
  (1) a wrong field type in `scoring.yaml` went undetected, so every class's field types are now compared with a
  frozen copy of the old registry (`tests/fixtures/old_registry_field_types.json`; a deliberate wrong type is caught);
  (2) a plug-in scorer returning 2.0, NaN or None flowed into scores, so plug-in results outside a finite [0, 1]
  now raise `ScorerResultError`; (5) README and plan prose corrected.
- Left as known minors: both packages register a global Hypothesis profile, so in a combined root run the last
  conftest loaded wins (record's property tests ran with 60 examples instead of 40); a non-string key in a
  prediction fails with `AttributeError` as in the old Dojo; a `registry` built for one profile is not checked against
  another.
- R6 is partly met by design: the Dojo consumes labels through structural Protocols. Nothing here reads the
  mailroom-dataset or interim labels yet, because `mailroom_labels` is built on `claude/project-thread-pbvxf9`.
  The Protocol field names came from that thread's message and have not been checked against its code.
- Mutation checks by the implementers: 17 of 17 (A), 22 of 22 (B, after adding tests for 7 survivors), 23 of 23 (C);
  the reviewer's own 12 source and 5 profile mutations were caught except the profile-type one, now fixed.
- Not verified: the real sentence-transformers model (fake backend only); hosted CircleCI (the new dojo step has not run).

## Next (in order)

1. Check CircleCI on the PR head; bot findings are claims to verify, not orders.
2. Rebase or merge once the record branch and the labels branch (`claude/project-thread-pbvxf9`) have landed:
   check `labels_interface` Protocols against the real `mailroom_labels.LabelSet`/`DocumentLabels`/`FieldLabel`
   (names were taken from a message, never checked against code), add the workspace test that
   `config/clients/<client>/profile.yaml` (labels) and `scoring.yaml` (Dojo) agree on class keys, field names and
   types, and consider reading the field-type list from `mailroom_dictionaries`.
3. Merge only after CircleCI is green on the final head, reviews are resolved and a link sweep is clean; the
   record PR merges first. Squash with a clean message.
4. Later chunks: suites and tasks, metric registry, label policy, calibration, serving and cost, and the stage
   gate re-scoring the imported v1 and slice-100 r2 runs once the run record importer (Task 10) has landed.

## Notes

- Old Dojo can be run read-only for regeneration: see `packages/dojo/docs/golden-parity.md`. Never edit it, and
  never work its open issues (another agent owns them).
- The human review desk node requested by JJB is a separate engine/launcher-level project; it has not been started here.

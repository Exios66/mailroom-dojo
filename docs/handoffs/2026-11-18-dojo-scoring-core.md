# Handoff: Dojo scoring core (project 3, chunk 1)

Date: 2026-11-18. Repo: Exios66/mailroom-dojo. Branch: `claude/project-thread-fmjkqn` (pushed), built on the
run-record branch `claude/project-thread-h3rj4z`. PR: not opened yet (see Next).

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

## State at checkpoint

- Task B (extraction layer) was in its final mutation-check pass when work was checkpointed; its tests and
  fixtures are committed. Before trusting the tree, run the whole suite on a clean tree:
  `.venv/bin/python -m pytest packages/dojo/tests packages/record/tests -q -n 4`.
- No independent Opus review has been run yet on the combined diff. The implementers' own mutation checks
  caught 17 of 17 (A) and 23 of 23 (C) deliberate bugs; B's result is in its commit messages or must be re-run.
- Not verified: real sentence-transformers model (fake backend only); the new CircleCI step has not run in hosted CI.

## Next (in order)

1. Run the full suite on a clean tree; fix anything red.
2. One independent Opus reviewer (usage is tight) on `git diff origin/claude/project-thread-h3rj4z...HEAD`
   with the lens "does it solve the problem, and what breaks"; fix valid findings; re-run the suite.
3. Rebase or merge once the record branch and the labels branch (`claude/project-thread-pbvxf9`) have landed:
   check `labels_interface` Protocols against the real `mailroom_labels.LabelSet`/`DocumentLabels`/`FieldLabel`
   (names were taken from a message, never checked against code), add the workspace test that
   `config/clients/<client>/profile.yaml` (labels) and `scoring.yaml` (Dojo) agree on class keys, field names and
   types, and consider reading the field-type list from `mailroom_dictionaries`.
4. Add `classification/` and `labels_interface/` rows to `packages/dojo/README.md`.
5. Open the PR ready for review (JJB asked for ready PRs so the bots run) with a clean body: no claude.ai link,
   no Claude Code footer, no Claude-Session trailer. Strip any footer the tool appends with `update_pull_request`.
   Merge only after CircleCI is green on the final head, reviews are resolved and a link sweep is clean; the
   record PR merges first.
6. Later chunks: suites and tasks, metric registry, label policy, calibration, serving and cost, and the stage
   gate re-scoring the imported v1 and slice-100 r2 runs once the run record importer (Task 10) has landed.

## Notes

- Old Dojo can be run read-only for regeneration: see `packages/dojo/docs/golden-parity.md`. Never edit it, and
  never work its open issues (another agent owns them).
- The human review desk node requested by JJB is a separate engine/launcher-level project; it has not been started here.

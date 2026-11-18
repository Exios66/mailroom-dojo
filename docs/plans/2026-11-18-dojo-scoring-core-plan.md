# Plan: Dojo scoring core (project 3, chunk 1)

Date: 2026-11-18. Package: `packages/dojo` (`mailroom_dojo`). Branch: `claude/project-thread-fmjkqn`,
built on the run-record branch (`claude/project-thread-h3rj4z`); the labels package is built on
`claude/project-thread-pbvxf9` and lands separately.

## Scope

Port the Dojo's pure scoring layers from `llm-dojo-scoring` (at `23f8234c`, 0.30.1): field-type
scorers (`scoring/field_scoring.py` and what it needs), classification metrics
(`scoring/classification.py`) and the golden cases that exercise them. The inventory of what is
ported, rewritten or dropped, with file and line for every hard-coded class, field and setting, is
`docs/plans/2026-11-18-dojo-scoring-port-inventory.md`. It is the technical reference for this plan.

Out of scope (later chunks): suites and tasks, metric registry, serving, cost, calibration,
bootstrap, label policy, scorecard honesty, decision lanes, MAUD question scoring, the stage gate
that re-scores the imported v1 and slice-100 r2 runs (it waits for the run record importer, Task
10 on the record branch), and any work on open `llm-dojo-scoring` issues.

## Requirements served (spec R1-R10)

| Req | How this chunk serves it |
|---|---|
| R2 | Per-field scoring by type, coverage and precision kept apart, exact parity with the old scorers |
| R3 | Classification metrics and label normalization with a profile-driven class set |
| R4 | A new document type is a class entry in a scoring profile (YAML), no code edit. Proved by an end-to-end synthetic-type test that also asserts no source file names the type |
| R6 | Scoring reads expected values through a `LabelSource` Protocol that the labels package satisfies (statuses hub, interim, near_gold, gold; unverified documents flagged, never silently scored) |
| R8 | Parity tests against recorded old-Dojo outputs, differential corpus, property tests, and fault tests (failing similarity backend, missing optional dependency, malformed profile, hostile inputs) asserting exact reported behaviour |
| R9 | One-way dependencies, no globals, no environment variables, no network; boundary test |

## Decisions

1. **No process-global state.** The old `Settings` (lru-cached, mutated in place, env override) is
   replaced by an immutable `ScoringProfile` passed to every function that needs one.
2. **Client profile.** The labels thread owns `config/clients/<client>/profile.yaml` (classes,
   fields with types, vocabularies). The Dojo owns the scoring rules (thresholds, containment and
   coverage-scored fields, label folding, row variants, trace options, accepted unscored keys) in
   `config/clients/<client>/scoring.yaml`. Each package parses only its own file or section, so
   `record` stays domain-free and the engine never needs the Dojo's types. For now `scoring.yaml`
   also lists each class with its field types so the Dojo works before the labels package lands; a
   workspace test added when the labels package lands asserts the two files agree (class keys,
   field names, field types).
3. **Label interface.** `mailroom_dojo.labels_interface` holds structural Protocols (`LabelSource`,
   `ExpectedDocument`, `PresenceExpectation`); the labels package implements them without importing
   the Dojo. The old Hub-row shaping helpers (`scoring_gt_fields` and the presence builders) are
   ported as a clearly marked reference adapter, to move to `mailroom_labels`.
4. **Fail loud, not silent.** Missing `jellyfish` or `scipy` is an import error (hard
   dependencies), not a quiet change of algorithm. Unknown field types and malformed profiles raise
   `ProfileError` at load. The remote OpenRouter embedding call is dropped; embeddings are an
   optional injected backend, off by default.
5. **Parity.** Scores for every ported case are identical to the old Dojo's. Intended differences go
   in `packages/dojo/docs/score-changelog.md` and a machine-read `intended_differences.json`; the
   parity test fails on an unlisted difference and on a listed one that no longer occurs.
6. **Golden cases.** No old golden case calls the in-scope layers directly (they go through suites
   and tasks). So parity uses (a) the 704 recorded calls that cross into the in-scope modules during
   the old golden run, whose `field_scores` are tied back to `scores.json`, and (b) a differential
   corpus from the old direct tests plus seeded generated inputs (the audits, presence and dates that
   no golden reaches). Both are committed as data with the old commit SHA, the old Dojo version and
   the hashes of `inputs.json` and `scores.json`; a capture script reproduces them and never runs in CI.

## Tasks

- **A**: package scaffold, profile (loader, validation, label folding), value helpers,
  normalization, scalar scorers, scorer registry, similarity backend, CMS identifier plug-in, and
  `config/clients/mailroom-dataset/scoring.yaml`.
- **B** (needs A): entity lists, audits, presence, row variants, trace capture, `score_extraction`.
- **C** (classification and labels interface need only A's profile): classification package, label
  interface and reference adapter, golden capture script and fixtures, parity and differential
  tests, R4 end-to-end test, boundary test, score changelog.
- Review: independent Opus reviewers on the full diff (lens 1 and 2 together, lens 3 hostile), fixes,
  re-run of the whole suite.

Test strategy: `docs/test-strategy-dojo-core.md` (written in task A).

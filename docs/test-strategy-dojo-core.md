# Test strategy: `mailroom_dojo` scoring core

What the Dojo core must prove, how each claim is tested and where the test lives. The plan
is `docs/plans/2026-11-18-dojo-scoring-core-plan.md`; the port inventory is
`docs/plans/2026-11-18-dojo-scoring-port-inventory.md`. This file covers task A (profile,
values, normalization, scalar scorers, scorer registry, similarity backend, CMS plug-in);
tasks B and C and chunk D1 (statistics) add their sections.

## Principles

- **A wrong number here is a wrong number everywhere.** Parity with the old Dojo
  (llm-dojo-scoring 0.30.1, commit `23f8234c`) is proven on data, with exact floats, before
  anything is improved. Old behaviour that looks wrong is kept and listed under "Found, not
  changed" in `packages/dojo/docs/score-changelog.md`.
- **Failure is never silent.** A malformed profile raises `ProfileError` at the exact YAML
  path; an unregistered field type raises unless the caller asks for a reported fallback;
  a failing similarity backend keeps the string score and records a `SimilarityFallback`.
  Each failure has a test that asserts the exact exception or report, not just "it failed".
- **Invariants, not only examples.** `hypothesis` drives hostile inputs through every scorer
  (bounded float, no exception), every fold (result inside the vocabulary), the profile
  hash (stable under any key reordering) and the date parser (same answer under different
  fake clocks).
- **No global state, no network, no clock.** Checked statically (AST over the whole source)
  and dynamically (sockets forbidden while the corpus is scored; threads with two profiles).
- **Tests must fail when the code is wrong.** Mutation checks (below) show it.

## Claims and tests

| Claim | Test |
|:---|:---|
| Every scalar layer gives the old output exactly | `test_differential_scalar_parity.py`: 21,009 cases (2,748 calls captured from the old direct tests, the rest seeded generated inputs), 37 functions, compared as canonical JSON |
| The corpus is what it says | same file: header names the old commit and version; counts per function match the lines; both sources present |
| The mailroom profile equals the old registry | `test_profile_loading.py`: classes and order, live/retired, field maps (corporate_record's 40 fields), name rules, CMS variant, defaults |
| Every malformed profile is refused at its path | `test_profile_loading.py`: 80 failure cases (unknown keys at every level, unregistered types, label without vocabulary, duplicate class/variant/token/sorter label, variant fields not in class, NaN/inf/bool/text/out-of-range thresholds, reversed bands, bad aliases and catalogs, duplicate YAML keys, YAML 1.1 booleans as keys, invalid YAML) |
| A profile is immutable and identified by content | `test_profile_loading.py`: frozen attributes and mappings; hash equal across loads, file vs mapping, any key order (hypothesis); changes with any rule, value or ordered list |
| Label folding is closed and ordered | `test_label_folding.py`: each fold method, tie order, fixed points of every mailroom token, results inside the vocabulary for any input (hypothesis) |
| Scorers are bounded and behave as documented | `test_scalar_scorers.py`: documented examples, `[0, 1]` float for any hostile input, self-match, symmetry where it holds, the directional date rules |
| Dates never read the clock | `test_normalization.py`: fixed two-digit pivot under four fake years; property test under two fake clocks; a clock that raises is never touched |
| Hard dependencies fail loudly | `test_normalization.py`: importing without jellyfish, python-dateutil or PyYAML is an import error |
| Similarity backend failures are reported | `test_similarity_backend.py`: raises, NaN, text, bool, list, Decimal, out of range and infinity each keep the string score and record one fallback with exact reason and detail, plus a warning; None declines silently |
| Two profiles do not interfere | `test_similarity_backend.py`: eight threads, two thresholds, 2,400 scores, each thread sees only its own threshold and fallbacks |
| No network | `test_similarity_backend.py`: `socket.socket`, `create_connection` and `getaddrinfo` raise while the profile loads and a seventh of the corpus is scored |
| sentence-transformers is optional and local | `test_similarity_backend.py`: lazy import (subprocess check), `local_files_only=True`, missing extra named in the `ImportError`, bounded cache, clipped cosine |
| The registry resolves every mailroom type | `test_scorer_registry.py`: built-ins equal the loader's type names; plug-ins only when declared; fallback only when asked, and flagged |
| No global state | `test_no_global_state.py`: no environment reads, `global`, cache decorators, clock calls, network/thread/LLM/old-Dojo imports, or mutable module-level values; a positive control proves the checks see each pattern |

## Mutation check (task A)

A scratch script applied 17 single-line mutations to the source, one at a time, and ran the
suite: dropping a null token, ignoring NaN as empty, `<=` to `<` on the money tolerance,
the two-digit-year pivot, the date partial credit, the inventory alias length, the token
ranking order, the backend range check, the rescue threshold comparison, the NaN check,
the unknown-key check, the number range check, the band order check, unsorted hash keys,
an order-sensitive `FrozenMapping` hash, a money name word and the NDC padding. All 17
were caught.

## Task B: entity lists, audits, presence, trace capture, `score_extraction`

- **Parity.** `test_differential_extraction_parity.py` replays 6,414 recorded old-Dojo cases (the old golden
  run's `score_extraction` calls, the old direct tests' inputs and seeded generated inputs: role words,
  duplicates, empty lists, partial-GT and containment fields, CMS identifiers, grounded and ungrounded dates
  with `doc_text`, confidence and reasoning in many shapes) and requires canonical-JSON equality. The golden
  cases are tied to `scores.json` paths and hashes in the fixture header.
- **Behaviour under fault.** A scipy import failure reports the greedy matcher and why; any other matcher error
  propagates. A failing, NaN or out-of-range similarity backend inside lists and presence is reported, never
  swallowed. An unknown field type raises unless the caller opts into scoring it as `name`, and the fallback is
  listed on the result. Non-mapping arguments raise `TypeError` at once. Hostile shapes (None, dicts for lists,
  10k-item lists, huge strings) finish inside a time budget.
- **Properties.** Precision, recall and F1 stay in [0, 1] and obey their identities; matched counts are
  invariant under shuffles except where the old scorer's ties make them order-dependent (that case is pinned by a
  test and recorded as "found, not changed").
- **Mutation check.** 22 single-line mutations over the six modules: 15 caught first, one targeted test added per
  survivor, 22 of 22 on the second run.

## Task C: classification, label interface, golden parity, new types, boundaries

| Claim | Test |
|:---|:---|
| Every old golden boundary call gives the identical result | `test_golden_parity.py`: all 674 recorded calls (24 functions, the 16 `score_extraction` calls among them) replayed with the mailroom profile; each `field_scores` tied by hash to its `scores.json` paths (and to the real file with `OLD_DOJO_CHECKOUT`); an unlisted or stale entry in `intended_differences.json` fails |
| Classification gives the old outputs | `test_classification_parity.py`: 7,774 cases from the old direct tests and seeded sorter outputs, 14 functions |
| The label set comes from the profile | `test_classification.py`: the old class table reproduced, retired classes out, match order, sorter labels and synonyms, conflicting labels refused at their path, exact length-mismatch messages, hostile and deep-JSON outputs, two profiles in eight threads |
| Metric identities hold for any input | `test_classification_properties.py`: accuracy is trace over total, per-class counts are matrix rows and sum to the kept rows, macro values bounded, binary counts cover every row, row order changes nothing, F-beta bounds and symmetry |
| Labels are read structurally and never scored silently | `test_labels_interface.py`: the labels package's shapes satisfy the Protocols; only scorable fields kept; unverified refused unless flagged; bad status and unknown class refused; 1,194 Hub-row cases equal the old GT helpers |
| A new type is configuration only (R4) | `test_new_document_type.py`: a YAML class scored with exact numbers, source tree byte-identical and never naming it, broken profiles refused at their path, two new types at once, existing classification and extraction unchanged when classes are appended (and the first-match exception documented) |
| One-way dependencies (R9) | `test_boundaries.py`: static, relative and dynamic imports, environment, globals, caches and module-level mutables, with a planted positive control; a fresh interpreter importing every module loads no engine, old Dojo or network client |

Mutation check (task C): 23 single mutations over `classification/` and
`labels_interface/` (pattern joiner, retired filter, JSON lowercasing, stripping, synonym
case, label conflicts, None-as-text, accuracy denominator, F-beta guard, label order, tie
order, flag stripping, empty class, rate defaults, one-sided length check, scorable and
verified filters, status check, pending annotation, annotation keys, placeholder spans,
subclass token): all 23 caught. Fixtures and their regeneration: `packages/dojo/docs/golden-parity.md`.

## Chunk D1: statistics (bootstrap, calibration, paired tests)

`mailroom_dojo.statistics` ports the old `analysis/bootstrap.py` and
`analysis/calibration.py` and adds the paired comparisons the design's "Comparing runs"
layer names. The plan is section 6 of `docs/plans/2026-11-18-workspace-port-design.md`
(chunk 1, D1); its gate is "old tests and a differential corpus, identical".

| Claim | Test |
|:---|:---|
| Same numbers as the old Dojo on every valid input | `test_differential_statistics_parity.py`: 1,979 cases over the 16 old public functions (12 from the old golden run, 445 from eleven old test files, 1,522 seeded), results as canonical JSON, exceptions by type and message; every golden-run and old-test case is identical; the 209 differences are all invalid inputs, each listed with its reason and new outcome in `statistics_intended_differences.json` (an unlisted, stale or changed entry fails, with a positive control) |
| The old tests still hold | `test_statistics_old_cases.py`: the 6 cases of the old `test_bootstrap.py` and the 19 of `test_calibration.py`, same expected values, read through the new result types |
| Invalid input is refused with an exact error, or answered exactly | `test_statistics_faults.py`: empty and too-small input (None), NaN and infinities (refused naming the position, or dropped where the old Dojo dropped them), mismatched lengths, one-class populations (AUROC None), bad parameters and types, NumPy scalars; each error's type and full message |
| Invariants over random input | `test_statistics_properties.py` (hypothesis): the bootstrap interval holds the mean of symmetric data and narrows with nine times the documents; Wilson bounds hold the rate, shrink with more trials and are rounded outward; ECE and Brier in [0, 1] and the ECE is the bins' weighted gap; AUROC in [0, 1] and reverses with the scores; Spearman bounded and symmetric; the Clopper-Pearson bound is above the observed rate and loosens with confidence; `required_samples` is the smallest clearing size; `cutoff_at_target` equals a brute-force search; a seed fixes every result whatever ran in between |
| The new comparisons and per-group confidence are right | `test_statistics_comparison.py`: constant differences give a point interval; the paired interval equals `bootstrap_ci` on the per-document differences; pairing detects a 0.03 gain the unpaired test misses; unpaired documents dropped and counted; exact McNemar p-values by hand and against `scipy.stats.binomtest` up to 2,001 flips, symmetric in the runs; `calibration_by_group` by hand (agent and class keys, dropped rows, one-class groups) and equal to the population functions on random rows; exact errors for empty input, NaN, text, mismatched lengths and bad parameters; seed determinism, delta and p-value invariants (hypothesis) |
| No global state, network or environment | `test_boundaries.py` and `test_no_global_state.py` cover the new modules automatically (AST walk and fresh-interpreter import) |

Mutation check (D1): dropping the `- 1` from the lower percentile index in
`bootstrap.py` turns `test_new_code_reproduces_every_old_output[bootstrap_ci]` and
`[delta_significance]` red; taking the larger flip count's tail in `paired.py` (`max` for
`min`) turns tests red (hand-computed p-values, the scipy cross-check, the alpha test).
Both restored. The corpus regenerates byte for byte with
`packages/dojo/scripts/capture_old_dojo_statistics_cases.py` (header of the file).

## Running

```bash
.venv/bin/python -m pytest packages/dojo/tests -q -n 4               # whole suite, seconds
HYPOTHESIS_PROFILE=thorough .venv/bin/python -m pytest packages/dojo/tests -q -n 4   # ten times more examples
```

The capture scripts (`packages/dojo/scripts/capture_old_dojo_*.py`) need an old Dojo
checkout and its environment; they are maintenance tools and never run in CI.

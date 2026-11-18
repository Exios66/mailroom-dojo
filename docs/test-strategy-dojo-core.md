# Test strategy: `mailroom_dojo` scoring core

What the Dojo core must prove, how each claim is tested and where the test lives. The plan
is `docs/plans/2026-11-18-dojo-scoring-core-plan.md`; the port inventory is
`docs/plans/2026-11-18-dojo-scoring-port-inventory.md`. This file covers task A (profile,
values, normalization, scalar scorers, scorer registry, similarity backend, CMS plug-in);
tasks B and C add their sections.

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

## D2: `Measure` and the metric table

Plan: `docs/plans/2026-11-18-dojo-d2-measures-plan.md`.

| Claim | Test |
|:---|:---|
| A `Measure` is valid when built | `test_measure.py`: finite values, `n == 0` has no value, a missing value needs a reason, unverified labels carry no value, closed vocabularies, interval rules, any integral `n` (numpy too, never bool), `missing()` unverified by default; hypothesis property tests both ways |
| Only like is compared with like | `test_measure.py`: method, subject, cost basis, label status and each provenance key; `vary` keys present but free; `draw_seed` absent on both sides allowed; an unknown `vary` key raises |
| The table keeps every old id and golden metric | `test_metric_table_parity.py`: 184 registry ids in the old order plus ten golden-only metrics, the old fields kept (units through `UNIT_NORMALISED`), the 28 nested golden blocks and the chained composite accounted for, the owner-dropped intake diagnostics listed, against the re-captured `old_metric_registry.json` |
| Design needs are planned rows with readers | `test_metric_table_parity.py::test_design_rows_are_planned_needs_with_a_named_reader`: the 19 `design` rows, `dojo:<id>` ids, readers, paired keep-rule rows |
| The loader refuses a broken table at its path | `test_metric_table.py`: fault cases for every column, duplicate YAML keys, the roster, default views and guards, label layers, pairing, alias, `same_as` and design ids; `check()` refuses a fractional count, an interval outside the unit range or not around the value, a negative value outside `utility`, and a measure filed under an alias |
| Every live agent has a view; none for legacy or retired | `test_metric_table.py`: `test_every_live_agent_has_one_entry_and_no_legacy_or_retired_agent_has_one`, `test_no_default_or_guard_uses_a_directionless_or_dropped_metric`, `test_the_default_views_are_the_reviewed_ones` (views and roster against `fixtures/measures/default_views.json`); agent picks are read from the data, never named in code |
| Directions are stated and frozen; names are only linted | `test_metric_directions.py`: every direction equals `fixtures/measures/metric_directions.json`; the whole-word name lint reports exactly the 48 reviewed exceptions; whole words only; keep-rule rows have a direction |
| One spelling per unit | `test_metric_table.py::test_the_unit_vocabulary_is_closed` |
| Every column moves the content hash | `test_metric_table.py::test_every_column_is_part_of_the_hash` (one valid single-column edit each) and `test_the_canonical_form_writes_every_row_and_view_field` |
| Label layers are the labels package's | `test_metric_table.py::test_label_layers_are_the_labels_package_layers_and_derived_gold` (skips without `mailroom_labels`) |
| An unstated ground truth may need labels | `test_metric_table.py::test_an_unstated_ground_truth_is_unknown_not_label_free`, `test_every_kept_number_states_its_ground_truth`; `test_metric_table_parity.py::test_every_stated_ground_truth_was_unset_in_the_old_registry` |
| Dashboard queries show kept rows | `test_metric_table.py::test_dashboard_queries_leave_out_dropped_rows_unless_asked` |
| Shared ids keep their meaning; intake has its own row | `test_metric_table.py::test_intake_is_judged_on_its_own_class_accuracy_and_accuracy_keeps_its_meaning` |
| Every default-view and guard metric says how it is compared | `test_metric_table.py::test_every_default_and_guard_metric_names_its_pairing_or_why_not`, the "default without pairing" and "guard without pairing" faults, `test_a_whole_set_statistic_has_no_per_document_pairing` |
| Pairings are D1's methods | `test_metric_table.py`: each pairing maps to a D1 method string (the D1 import test skips until D1 is on the branch) |

Mutation check (D2): each single mutation below, applied by a scratch script that restores
the file and prints its hash, turned the suite red.

| Mutation | Result |
|:---|:---|
| comparability ignores the subject | 4 failed |
| whole-count check removed | 2 failed |
| interval-contains-value check removed | 1 failed |
| "a kept row names its pairing" removed | 2 failed |
| alias check removed | 4 failed |
| a nested golden block renamed in the capture | 2 failed |
| "every live agent has an entry" removed | 1 failed |
| "no view for a legacy or retired agent" removed | 2 failed |
| "a view or guard has a direction" removed | 2 failed |
| `usd` put back in the unit vocabulary | 1 failed |
| "a `same_as` target is canonical" removed | 1 failed |
| the signed-unit exception removed (`utility`) | 1 failed |
| the name lint matching substrings | 3 failed |
| the boolean 0-or-1 check of `check()` removed | 1 failed |
| one of `same_as`, `alias_of`, `pairing_note`, `readers`, `status`, a view's `label_layers` or `agent_families` dropped from the canonical form | 1 failed each |
| "a default view or guard names its pairing" removed | 2 failed |
| a reviewed name-lint exception renamed | 1 failed |

Earlier mutations of code the review replaced (the derived-direction check, the
headline-view rules) are in the git history of this file.

## Running

```bash
.venv/bin/python -m pytest packages/dojo/tests -q -n 4               # whole suite, seconds
HYPOTHESIS_PROFILE=thorough .venv/bin/python -m pytest packages/dojo/tests -q -n 4   # ten times more examples
```

The capture script `packages/dojo/scripts/capture_old_dojo_scalar_cases.py` needs an old
Dojo checkout and its environment; it is a maintenance tool and never runs in CI.

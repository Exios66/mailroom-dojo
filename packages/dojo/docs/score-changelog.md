# Score changelog

Every way `mailroom_dojo` scores differently from the old Dojo (llm-dojo-scoring 0.30.1,
commit `23f8234c`), and every old behaviour that looks wrong but was kept on purpose.

## Intended differences (task A)

None of these changes a score on the differential corpus or on any old golden case: no
golden case uses an embedding backend.

- **Embedding rescue is injected, never fetched.** The old Dojo built a remote embedding
  matcher itself when none was passed (settings and environment driven). The new Dojo
  never builds one: with no backend, name and free-text scoring is string-only.
- **A backend answer that is not a number in `[0, 1]` is not used.** The old rescue
  returned `max(string_score, float(similarity))`: a backend answering `1.5` produced a
  score of 1.5, the text `"0.9"` and `True` were converted to numbers, NaN silently kept
  the string score, and a list raised `TypeError` out of the scorer. The new rescue keeps
  the string score in every one of these cases and records a `SimilarityFallback`
  (`out_of_range`, `not_a_number` or `backend_raised`) on the `EmbeddingRescue`, logged as
  a warning.
- **The backend receives text.** The old rescue passed the raw values; the new one passes
  `str(predicted)` and `str(expected)`, the same strings its blank check already used.

## Found, not changed (task A)

Old behaviour kept for parity, pinned by tests or by the differential corpus wherever the
old output was deterministic, so changing one is a deliberate, listed score change.

- **`parse_money` reads `"nan"`, `"inf"` and `"1_000"`** as floats (Python's `float()`
  accepts them). A money field whose text is "nan" parses to NaN and never matches.
- **The money tolerance is applied in binary floating point.** `$100.00` against `$100.01`
  is a miss (`100.01 - 100.00 > 0.01`), while `$0.00` against `$0.01` is a match.
- **`frozenset()` is not empty.** `is_empty_value` names list, dict, tuple and set only.
- **Scalar scorers read `None` as the word "None".** `score_name_field(None, "None")` is
  1.0, and a `None` side can be offered to embedding rescue as `"None"`. Callers drop
  empty values before scalar scoring, so this is reached only by direct calls.
- **A label with no ASCII letter or digit never matches, even itself** (`"¹"`, `"日本"`):
  labels compact to `[a-z0-9]`.
- **The catalog subclass fold compares the compacted text with the raw catalog key.** A
  key with an underscore (`license_agreement`) is never matched by that step; such values
  reach their key through the alias or label steps instead.
- **`heuristic_field_type` matches name substrings**: `valid` is typed `id` and
  `candidate` is typed `date`.
- **The allowed-set subclass fold** took the first set member with the same compact form
  in set iteration order, which could differ between runs. The new fold keeps a token
  order, and the loader refuses two tokens with the same compact form, so the order never
  decides.

## Intended differences (task B)

None of these changes a score on the extraction corpus (6,414 cases) or on the 16
`score_extraction` calls of the old golden run.

- **The list matcher is chosen, not fallen back to.** The old `score_entity_list` fell back
  to a greedy matcher on *any* exception while building or solving the similarity matrix,
  so a scorer bug or a NaN similarity silently changed the algorithm. Now the Hungarian
  matcher is the default; the greedy matcher runs only with `matcher="greedy"`, or with
  `fallback_matcher="greedy"` when scipy cannot be imported (reported on
  `EntityListScore.matcher_fallback` and logged). Every other error propagates; a NaN
  element score makes the solver raise `ValueError`.
- **Malformed arguments fail at once.** A truthy `predicted`, `expected` or
  `audit_predicted` that is not a mapping, a `field_types` that is not a mapping, or a
  field type that is not text raises `TypeError` naming the argument. The old code raised
  `AttributeError` or `ValueError` from deep inside, or for some shapes returned a result
  (a list of pairs as `predicted` with no expected fields). A falsy non-mapping
  `audit_predicted` (`[]`) now means "no audit source"; the old code raised. A non-dict
  mapping as `expected` can now mark a row variant (the old check required a `dict`).
- **Unregistered field types are refused.** The old scorers scored any unknown type in a
  caller's map as `name`. Now `UnknownFieldTypeError` is raised unless the caller passes
  `unknown_field_types="score_as_name"`, which keeps the old scores and lists each such
  field in `ExtractionScoreResult.field_type_fallbacks`.
- **No field-type map means the profile's.** `field_types=None` takes the class's types
  from the profile (the old function required a map); a class the profile lacks is then a
  `KeyError`.
- **Unmapped keys follow a policy.** `unmapped_fields: heuristic` (the mailroom profile)
  types a key by its name as before; `skip` and `error` are new.
- **Presence has no built-in default field.** The old scorer assumed `cuad_clauses` for an
  expectation without a `field`; now the caller passes `default_field`, or a `ValueError`
  names the category. The unused `field_types` argument is gone.
- **Presence similarity answers are checked.** The old presence matcher used
  `float(similarity) >= threshold`, so `1.5` or the text `"0.9"` matched and `True` counted
  as 1.0. Now a `PresenceSimilarity` accepts only real numbers in `[0, 1]`; anything else
  (and a backend that raises) does not match and is recorded as a `SimilarityFallback`.
- **Logging.** Warnings go through the standard `logging` module (the old code used
  structlog events `bipartite_matching_failed` and `presence_embedding_similarity_failed`).

## Found, not changed (task B)

- **A list score can depend on item order.** The Hungarian matcher maximises the total
  similarity, not the number of pairs that pass the threshold. When two pairings have the
  same total, the solver picks by input order, and the matched count can differ:
  `["alpha beta", "beta"]` against `["beta alpha beta", "alpha gamma"]` matches 1, the same
  items in the other order match 0 (free text). In the old Dojo 45 of 20,000 random
  two-to-three-item lists over a four-word vocabulary showed this. Lists of all-or-nothing types (`id`, `label`,
  `cms_doc_ref`) are order invariant; a property test checks it.
- **The marker fallback and the predicted-value type override apply to every row of the
  class.** A blank predicted `cms_source_table` is read from `claim_type`, and a table token
  predicted on `claim_type` is scored as an exact label, even on a row not recognised as
  CMS and with `apply_variants=False`.
- **A variant-only field is typed on every class.** An expected `cms_source_table` with no
  type in the caller's map is scored as a `label` whatever the class (the old code named
  the field in `score_extraction` itself).
- **The audit uses the map's types, not the variant's.** On a CMS row,
  `supporting_documents` is scored as CMS identifiers but audited as names, and a scalar
  audit compares with the expected value as stored (a JSON text is not parsed).
- **Class keys are matched three ways.** Accepted-unscored keys (`maud_clauses` on
  `contract`) are found case-insensitively, label folding strips and lowercases the class
  key, while row variants and type overrides need the exact key.
- **Reasoning written as text breaks presence routing.** With routing on, a `reasoning`
  that is a non-empty string or list raises `AttributeError` (it has no `entries`
  mapping).
- **A single pair and a matrix treat NaN differently.** A one-to-one list compares a NaN
  element score with the threshold (false, so unmatched); a larger list now raises.


## Review fix: plug-in scorer results are checked

Not a score change for any built-in scorer (they are not wrapped and stay proved by parity). A plug-in scorer
(for example a VIN or policy-number scorer a client adds) now raises `ScorerResultError` when it returns anything
but a finite number in `[0, 1]`. Before the check, 2.0 flowed into the field scores, NaN into `overall_score`
(and made the solver refuse an entity-list matrix, which the old Dojo answered by silently switching to the
greedy matcher), and None failed with an unhelpful `TypeError`.

## Review fix: an integer too large for a float

`parse_money` on an int such as `10**400` raised `OverflowError` in the old Dojo (a JSON number can be that
large). It now returns signed infinity, like the text `"1e999"`, and a money field holding it scores 0.0 against
anything, as `"inf"` does. No recorded or generated parity case contained such an int, so no fixture changes.

## Review fixes: backend integers, supplied scorers, and two old behaviours kept

- A similarity backend returning an integer too large for a float is now reported as an `out_of_range`
  fallback (the detail is cut to 80 characters) instead of raising `OverflowError`.
- A supplied plug-in scorer replaces the bundled one by presence, not by truthiness.
- **Found, not changed:** `binary_metrics(..., normalize=True)` compares the normalized value with `positive`
  exactly as given, so `positive` must be a class key (its docstring says so; a sorter label or a mixed-case
  name never matches). The row-variant marker token is read with `str(value or "")`, so a JSON number `0`
  becomes an empty token (no mailroom marker is numeric). Both are the old Dojo's behaviour.

## Review fixes, round 3

- `parse_json_container` returns JSON text nested deeper than the interpreter allows unchanged; the old Dojo
  raised `RecursionError` out of extraction scoring on it.
- **Found, not changed:** the `catalog` fold's normalizer maps a mixed-case or punctuated catalog key only through
  the key, the aliases or the label prefix, as the old `normalize_corpus_subclass` did; `fold` and `normalize`
  can therefore differ for such a key. Differential parity covers the old behaviour, so it stays.

## Review fixes, round 4

- `parse_confidence` reads an integer too large for a float as no confidence; the old Dojo raised
  `OverflowError` out of trace capture.
- **Found, not changed:** `date_phrase_contained` tests the year-month pattern `\d{4}-\d{1,2}` against a key whose
  hyphens were already turned into spaces, so that branch of `explicit_month` never matches (the old Dojo is the
  same). Changing it would score some contained year-month dates differently, which is a scoring change for the
  Dojo owner to make first, not a port.

## D2: `Measure` and the metric table

### Intended differences (D2)

- **A measure over nothing has no value.** A `Measure` with `n == 0` carries `value=None` and a
  reason; it is never `0.0`. Some old scorers returned `0.0` for an empty input (an empty
  document set or a field set with nothing scorable), which reads as "everything wrong". D2
  defines the rule; no scorer is ported in D2, so no score changes yet. D3b applies it when it
  ports the suites, and lists each scorer it changes here.
- **`contracteval_false_no_related` is an alias** of `false_no_related_rate` (same unit,
  direction and aggregation). The table resolves the alias; a `Measure` filed under it is
  refused.
- **`composite`** (the chained summary's weighted score) is a table row; the old registry had
  no id for it, though the golden baseline emits it.

- **Gold for the decision agents is derived, not human-only.** `boss_wrongful_approve_rate`
  and `boss_needless_review_rate` were "human outcome only"; the judge's and arbiter's gold
  verdicts used a human resolution first. By the owner's decision (2026-11-18) gold no longer
  waits for human resolutions: each of these rows (and the sorter reviewer's) states in its
  `status_reason` how its gold is derived from the class and field labels, with the bands from
  the scoring profile. No scorer is ported in D2; D3b and D6 apply the rule.
- **The intake clerk diagnostics are dropped** (owner decision): `intake_prep_completeness`,
  `intake_changed_rate`, `intake_messy_rate`, `intake_hyphen_unwraps` and
  `intake_collapsed_blanks` are `not_ported`. The old golden intake suite emits them, so D3b
  lists them as intended differences of the parity gate.
- **The headline `bert_elapsed_ms` is dropped with the BERT fast path.** It was an old tier-0
  (headline) metric of intake; the port design drops llm-mailroom's BERT intake gate (section
  7), so the row is `not_ported`. The dashboard queries (`headline_rows`, `rows_for_agent`,
  `headlines_by_agent`) leave `not_ported` rows out unless called with
  `include_not_ported=True`, so intake shows no headline from the old tier 0.
- **An unstated ground truth is unknown, not "no labels".** The old registry left
  `ground_truth` unset on some rows. The table writes that as `null` and reads it as unknown:
  a default view whose metric has a `null` ground truth may name label layers, and
  `MetricTable.check` refuses `label_status="none"` for it. The nine kept numeric rows with
  no stated ground truth (unset in the old registry, or a golden-baseline row it never
  listed: `no_related_rate`) now state it: `maud_category_accuracy`, `character_accuracy`,
  `per_field_scores` and `extraction_category_presence` are `required` (scores against
  gold); `llm_call_count`, `total_tokens`, `classification_attempts`,
  `extraction_attempts` and `no_related_rate` are `none` (counts and a behaviour rate over
  every pair). No value changes; only the label rule a measure of them must keep.
- **Units have one spelling.** `usd` is written `USD` and `int` `count`; `confusion_matrix`,
  `per_class_stats` and `bootstrap_ci` are `structure`, not `float[0,1]`. The meaning is
  unchanged; a `structure` row is never reported as one number.
- **Duplicates name their canonical row** (`same_as`, `alias_of`). The old note on
  `contracteval_false_no_related`, "mirrors ContractEval no_related_rate", is wrong: it is
  `false_no_related_rate` (positive pairs), not `no_related_rate` (all pairs). The note keeps
  its old text; the row's `status_reason` corrects it.
- **18 planned design rows** (`origin: design`, ids `dojo:<id>`) add numbers the old Dojo never
  had; none changes an old number.

### Correction

Commit `56a5a5e` said "No score-changelog entry: no metric's meaning changed", and `ab609ff`
listed the `n == 0` rule as ported. Both are wrong: the `n == 0` rule is new (above), and the
alias and the composite row are table changes. The commits are not amended; this entry is the
correction.

### The old `scorecard_honesty.py`

Old module: `llm_dojo_scoring/metrics/scorecard_honesty.py` at `23f8234c` (1,176 lines). "D2"
means ported in this chunk (`mailroom_dojo.measures`); D3b, D4, D5, D6 and D7 are the owning
build chunks of the port design (section 6); "dropped" gives the reason.

| Old public name (line) | Disposition |
|:---|:---|
| `SCORER_VERSION` (56) | D2 shape: `Provenance.scorer_version`; D5 stamps the value from the run record |
| `PROVENANCE_KEYS` (58) | D2: `PROVENANCE_KEYS` (less `metric_id`, which is `Measure.method`) and the `Provenance` fields |
| `COST_BASIS_TOKEN_PRICE`, `COST_BASIS_VALUES` (74, 76) | D2: `COST_BASES` (the three values plus `none`) |
| `ERROR_CLASS_LENGTH_FINISH`, `ERROR_CLASS_CONTEXT_OVERFLOW` (81, 82) | D5: completion error classes of the run layer |
| `DEFAULT_SCHEMA_GATE`, `DEFAULT_INSURANCE_SCHEMA_GATE`, `SCHEMA_PROMOTION_GATES` (207-213) | D5, with the thresholds moved to `config/` (tuning is configuration) |
| `MAUD_HEADLINE_FIELDS` (428) | D3b: MAUD suite headline scope |
| `IncomparableMetricsError` (231) | D2: replaced by `IncomparableMeasuresError` (`method_mismatch`) |
| `UnknownMetricNameError` (235) | D2: replaced by `UnknownMetricError` |
| `ProvenanceExportError` (243) | D2: replaced by `IncomparableMeasuresError` (`provenance_missing`, `provenance_mismatch`, `cost_basis_mismatch`) |
| `GtAssessment` (248) | D2 shape: `Measure.label_status` and `reason`; D4 computes the assessment |
| `metric_id_for` (260) | D2: `MetricTable.get(id).metric_id` (an unknown id raises, as before) |
| `metric_ids_for_class`, `metric_id_allowed` (288, 293) | D7: which metric ids a class's comparison may use belongs to run comparison |
| `assert_comparable_metric_ids` (301) | D2: `assert_comparable` (`method_mismatch`) |
| `assess_extraction_gt` (362) | D4: scorable-field rules over the label set (fail closed) |
| `unscorable_extraction_result` (405) | D3b: suite output, built from `Measure.missing` |
| `headline_scope` (515) | D3b: which live fields a class headline covers |
| `intent_split` (612) | D3b: intent reported apart from the other fields |
| `aggregate_quality_honest` (674) | D2 rule (unscorable rows carry no value; nothing scored gives no value); D3b aggregates |
| `export_quality_verdict` (747) | Dropped: "unproven" is a `Measure` with `value=None` and a reason; "scored" is one with a value |
| `score_format_layer` (767) | D5: rows `parse_ok`, `schema_valid`, `schema_adherence` (layer D5) |
| `check_schema_promotion_gate` (814) | D5, threshold from `config/` |
| `classify_extraction_failure` (843) | D6: cause codes (format against capability) |
| `score_empty_field_contract` (859) | D3b: with `extraction_binary_metrics` (rows `extraction_f1`, `extraction_f2`) |
| `detect_maud_gt_ambiguity` (927) | D3b: MAUD question scoring |
| `canonical_error_class` (965) | D5: run-layer completion |
| `summarize_run_completion` (988) | D5: run-layer completion (rows `success_rate`, `error_rate`) |
| `aggregate_quality_itt` (1023) | D5: intention-to-treat quality over attempted documents |
| `stamp_provenance` (1055) | D2 shape (`Provenance`); D5 stamps it from the sealed record |
| `validate_comparison_provenance` (1103) | D2: `comparability_problems(..., vary=...)`: every key on both sides and equal, `vary` keys present but free, `draw_seed` absent on both allowed, cost basis equal |
| `normalize_serving_kind_token` (1165) | D5: serving by backend (`Provenance.serving_kind` is compared as given until then) |

The old tests, `tests/test_v018_scorecard_honesty.py` (13 test functions):

| Old test (line) | Disposition |
|:---|:---|
| `test_unscorable_triage_only_contracts_gt_not_zero` (29) | D4 (assessment) and D3b (suite output); the "no silent zero" half is D2: `test_unverified_labels_never_carry_a_value`, `test_a_missing_value_needs_a_reason` |
| `test_metric_id_mismatch_incomparable` (55) | D2: `test_measures_that_differ_in_identity_are_not_compared[method_mismatch]`; the `compare_serving` half is D5 |
| `test_schema_valid_gate_insurance` (68) | D5 |
| `test_maud_collapsed_gt_ambiguous` (80) | D3b |
| `test_maud_distinct_subquestion_keys_score_normally` (111) | D3b |
| `test_format_vs_extraction_and_empty_fields` (151) | D3b (empty fields) and D6 (failure class) |
| `test_prose_wrapped_json_is_parse_fail_not_zero_extraction` (169) | D5 (format layer) and D3b (extraction P/R/F) |
| `test_correctly_empty_fields_score_one` (188) | D3b; the `score_extraction` half already passes in chunk 1 (`field_scoring`) |
| `test_normal_finish_reasons_count_as_completed` (221) | D5 |
| `test_normal_finish_reasons_preserve_error_signals` (242) | D5 |
| `test_error_finish_reasons_count_as_errors` (261) | D5 |
| `test_completion_and_cost_basis` (269) | D5 (completion, ITT, cost basis per record); "never mix cost bases" is D2: `cost_basis_mismatch` |
| `test_modal_serving_kind_not_local` (308) | D5 |

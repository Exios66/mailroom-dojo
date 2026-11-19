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
  name never matches). This is the old Dojo's behaviour. (The row-variant marker token noted here at first
  was changed later; see "Review fixes, round 5".)

## Review fixes, round 3

- `parse_json_container` returns JSON text nested deeper than the interpreter allows unchanged; the old Dojo
  raised `RecursionError` out of extraction scoring on it.
- **Found, not changed:** the `catalog` fold's normalizer maps a mixed-case or punctuated catalog key only through
  the key, the aliases or the label prefix, as the old `normalize_corpus_subclass` did; `fold` and `normalize`
  can therefore differ for such a key. Differential parity covers the old behaviour, so it stays. (Changed
  later for keys the old Dojo could not hold; see "Review fixes, round 5".)

## Review fixes, round 4

- `parse_confidence` reads an integer too large for a float as no confidence; the old Dojo raised
  `OverflowError` out of trace capture.
- **Found, not changed:** `date_phrase_contained` tests the year-month pattern `\d{4}-\d{1,2}` against a key whose
  hyphens were already turned into spaces, so that branch of `explicit_month` never matches (the old Dojo is the
  same). Changing it would score some contained year-month dates differently, which is a scoring change for the
  Dojo owner to make first, not a port.

## Review fixes, round 5

- **A numeric row-variant marker is read as its text.** The marker token was read with `str(value or "")`, so
  an expected JSON number `0` (or `False`) became an empty token and a profile marker `"0"` never matched it.
  It is now `""` only for None and an empty list, dict, tuple or set; a number or boolean keeps its text. The
  old Dojo had one fixed marker field whose values are table names, so it could not express such a marker. No
  score changes for the mailroom profile: its marker values are `carrier`, `inpatient`, `outpatient` and `pde`,
  none of which is the text of a number or a boolean, and both the empty and the new token miss them. The
  fixtures and the intended-differences ledger are unchanged.
- **A catalog normalizer finds a token by its compact form last.** When the key, alias and label steps all
  miss, `normalize` now returns the token whose compact form equals the value's (as `fold` does) instead of the
  unknown token, so a key such as `Co-Op` whose label does not start like it is no longer unknown. The three
  old steps run first and unchanged, so only a value that used to get the unknown token can move. No mailroom
  score moves: every mailroom token already normalizes to itself
  (`test_every_mailroom_vocabulary_is_closed_and_has_fixed_points`), and the normalizer reads a value only
  through its compact form, so any value with a token's compact form already reached that token before the
  new step. The old Dojo's catalogs were fixed lowercase tables and could not hold such a key.
- **Found, not changed** (each checked against the old Dojo at `23f8234c` on the same input, identical output):
  - `cms_document_signature`: when the word NDC appears, the first run of 9 to 11 digits is read as the NDC, even
    a claim number before it (`"claim 1234567890 NDC 00703051002"` is NDC `01234567890`, so it misses
    `"NDC 00703051002"`).
  - Presence routing: a reasoning entry tagged with the category whose evidence and section reference are both
    blank still counts as routed, so the field's own candidates are ignored and the category scores false.
  - Recall-coverage lists: an empty predicted item (`[""]`) is not a role phrase, so it "names a party" and
    role-word expectations (`Seller`) get recall credit.
  - `date_grounded_in_document` finds only full dates in the document (numeric, month-day-year, day-of-month),
    so a prediction "August 2014" is not grounded by a document that says "August 2014".
  - Trace capture: a reasoning value that is a single mapping with only a `span` key is dropped (a bare entry
    needs `field` or `evidence`), and `missing_confidence` `assume_1` or `assume_0` fills the confidence for the
    gate only, so the calibration error stays None. The `TraceCaptureOptions` docstring, which said the policy
    also applied to calibration, now says what the code does; the old Dojo's comment made the same wrong claim.


## Review fixes, round 6

- **A huge backend integer never crashes a fallback.** Not a score change: the fallback reason only. A
  `PresenceSimilarity` backend returning an integer too large for a float (`10**400`) raised `OverflowError` out
  of `covers`; it is now an `out_of_range` fallback, as it already was for embedding rescue. An integer over
  Python's digit limit (`10**5000`) made the fallback detail itself raise `ValueError` in both places; the detail
  now reads `<int too large to show>`. The old Dojo raised on both (`float(similarity)`); in every case the
  string or containment score is kept, so no score moves and no fixture changes.

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

- **Gold for the decision agents is derived, not human-only, in new rows.** By the owner's
  decision (2026-11-18) gold no longer waits for human resolutions: it is derived from the
  class and field labels of `hub`, `interim`, `near_gold` and `review`, with the bands from
  the scoring profile. That is a new rule, and every kept id keeps its old meaning (port
  design, section 10, decision 4), so it lives in eight new design rows,
  `reviewer_class_accuracy_vs_gold`, `reviewer_wrong_accept_upper_95_vs_gold`,
  `judge_verdict_accuracy_vs_gold`, `judge_escalation_recall_vs_gold`,
  `arbiter_decision_accuracy_vs_gold`, `arbiter_wrongful_accept_rate_vs_gold`,
  `boss_wrongful_approve_rate_vs_gold` and `boss_needless_review_rate_vs_gold`, and the views
  of the sorter reviewer, judge, arbiter and boss show them. No scorer is ported in D2; D6
  applies the rule.
- **Correction (review round 3).** Until this round the derived rule sat in the
  `status_reason` of the eight old ids themselves (`boss_wrongful_approve_rate` "replaces the
  old 'human outcome only' rule"), so one id carried two rules. The old rows are restored to
  their old meaning (no derived-gold text; the judge rows' pairing and the arbiter and boss
  rows' pairing notes, added only for the views, are gone with it), the rule moved to the
  `_vs_gold` rows, and two tests keep it there: no row carried from the old registry or golden
  baseline states a derived-gold rule, and a view on `derived_gold` shows design rows only
  (also a loader rule).
- **Six golden values are intended D3b golden-parity differences (review round 3).** The old
  golden baseline emits them, but their rows are `not_ported`, so the D3b parity gate reports
  them as intended differences, not as failures:
  - `gpu_memory_used_gb`, `kv_cache_utilization`, `queue_time_seconds` and
    `prompt_tokens_per_second` under `suites.local_vs_api.score.api` and `.local`: serving
    telemetry of the old serving scorecard (vLLM and nvidia-smi readings) that the new engine
    does not emit and nothing reads;
  - `false_negative_rate` and `false_positive_rate` under
    `task_kinds.legalbench.score.binary`: LegalBench's binary error rates, which no gate,
    report or projection reads.

  `tests/test_metric_table_parity.py` checks that every row a per-side or binary block names
  is kept, one of these six, or a run setting or sample size (a Measure's provenance or `n`).
  The earlier note that the per-side blocks hold only "figures that are rows already" was
  wrong for these four.
- **The intake clerk diagnostics are dropped** (owner decision): `intake_prep_completeness`,
  `intake_changed_rate`, `intake_messy_rate`, `intake_hyphen_unwraps` and
  `intake_collapsed_blanks` are `not_ported`. The old golden intake suite emits them, so D3b
  lists them as intended differences of the parity gate.
- **Intake is judged on its own row, `intake_class_accuracy`; `accuracy` keeps its meaning.**
  By the owner's decision intake is judged only by its triage class against the gold class,
  with no guard. In the old intake suite `accuracy` is the exact match of the cleaned text
  against the clerk's gold and `f1_macro` the token F1 (`pipeline/intake.py` `_score_one`;
  golden `suites.intake.score.accuracy = 1.0`), so the class accuracy is a new planned row
  (`dojo:intake_class_accuracy`, direction higher, unit proportion, gold from the class
  layers) and intake's default view. The old suite numbers stay as they are.
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
- **A denominator the run sets is not paired (review round 3).** A `denominator` column
  (`gold` or `run`) says whether the evaluation set or the run's own decisions set a row's
  denominator. The loader refuses a pairing on a `run` row and needs `gold` on every
  `paired_percentile_bootstrap` row. `wrong_archive_rate` (over the run's scorable archives),
  the judge's `_vs_gold` verdict accuracy and escalation recall, and the sorter reviewer's
  class accuracy, wrong-accept and routed-to-review rates (the old ids and the `_vs_gold` row)
  were paired; they now carry a `pairing_note`, and D7 builds the resample. No value
  changes; only how two runs' values may be compared.
- **The table is `version: 2`.** Its content changed after version 1 (the `_vs_gold` rows,
  the restored decision rows, the `denominator` column and the pairing notes above), so the
  version moved with it. The version and content hash are frozen in
  `tests/fixtures/measures/table_identity.json`; a test fails when the hash moves, and says
  so when the version did not.
- **27 planned design rows** (`origin: design`, ids `dojo:<id>`) add numbers the old Dojo never
  had; none changes an old number. Eight of them are the `_vs_gold` rows above.

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

## Intended differences (D1, statistics)

`mailroom_dojo.statistics` gives the old `analysis/bootstrap.py` and
`analysis/calibration.py` numbers on every valid input of the statistics corpus (2,000
cases, among them every encodable call of the old golden run and of eleven old test
files; the four old-test calls with NumPy arguments, `auroc` with a float32 and with an
int64 argument, `clopper_pearson_upper` with an int64 and `required_samples` with an
int32, are counted in the corpus header's `captured_test_skipped` and not replayed). The
195 cases that differ are all invalid inputs; each is listed with its new outcome in
`tests/fixtures/differential/statistics_intended_differences.json`. No golden score moves:
the golden run's 12 calls (`bootstrap_ci`, `delta_significance`) are identical.

- **A NaN or infinite score is refused.** `bootstrap_ci` and `delta_significance` raise
  `ValueError` naming the position (`values[1] is not a finite number: nan`); the old Dojo
  returned a NaN or infinite interval, a `significant` flag decided by where NaN sorted, or
  `None` when the other side was short. The text `"nan"` is read as NaN, so it is refused too.
- **A NaN or infinite confidence or score in a cutoff search is refused**
  (`cutoff_at_target`, `cross_fit_cutoffs`, `cutoff_for_precision`); the old Dojo sorted it
  to an arbitrary place and returned a cutoff that depended on where it landed. ECE, Brier,
  reliability bins, AUROC and Spearman still drop such values, as before.
- **An integer confidence too large for a float is dropped by ECE, Brier and reliability
  bins**, like any other confidence outside `[0, 1]`; the old Dojo raised `OverflowError`
  from `float(10**400)`. `bootstrap_ci` and `cutoff_for_precision` still raise it, as
  in the old Dojo and the corpus. The corpus holds no such calibration call, so the ledger has no entry;
  `tests/test_statistics_faults.py` pins it.
- **Resampling parameters are checked.** `n_boot` must be an integer of at least 1
  (`n_boot=0` raised `IndexError` inside the old resampler), `alpha` must be in `(0, 1)`
  (`alpha=1.5` gave an interval with `lo` above `hi`), and `seed` must be an integer (the
  old Dojo seeded `random.Random` with text such as `"abc"` and returned an interval). As in
  the old Dojo, fewer than two usable values give `None` first, so `bootstrap_ci([0.5],
  n_boot=0)` and `delta_significance([0.5], [0.1, 0.2], alpha=nan)` are still `None`; with
  enough values a bad parameter raises. A `decimal.Decimal` or `Fraction` `alpha` inside
  `(0, 1)` gives the old interval, as before. A NumPy integer `seed` or `n_boot` is read as a
  plain int: the old Dojo raised `TypeError` from `random.Random` for a NumPy seed and
  reported a NumPy `n_boot` as given, which `json.dumps` refuses; the numbers are those
  of the same plain int. The corpus holds no NumPy parameter, so the ledger has no entry.
- **A Wilson rate must be a rate.** `wilson_ci` and `wilson_bounds` refuse a `p` outside
  `[0, 1]`, NaN or infinite (`ValueError`), a bool `p` and a text `p` such as `"0.5"`
  (`TypeError`). The old Dojo answered `[0, 1]` for NaN, gave bounds for `p = 1.1`, raised
  `TypeError` comparing a complex number for `p = 3.0`, and read `"0.5"` with `float()`.
  A `decimal.Decimal` rate is read by `wilson_ci` as before; `wilson_bounds` still refuses
  it with a `TypeError` (the old one raised from `Decimal + float`), with another message.
- **A Wilson support must be a whole number.** With a rate, `n = 2.5` or `True` is a
  `TypeError` (the old Dojo computed with 2.5 trials and reported `n = 2`, and read `True`
  as one trial). An integer-valued float (`10.0`, NumPy's `float64(10.0)`, as workbook
  rows arrive) is ten trials, as before. `wilson_bounds(p, 0)` is a `ValueError` (it was
  `ZeroDivisionError`). Unchanged from the old Dojo, `wilson_ci` returns `None` before any
  check for any real `n < 1` (`0.5`, `-2.5`, `-inf`, `False`) and, when `p` is `None`, for
  any real `n` (`2.5`, `True`, NaN, `inf`). A NaN or infinite `n` with a rate is a
  `TypeError` (`n must be an integer; got nan`); the old Dojo raised too, a `ValueError`
  or `OverflowError` from `int()`. A `decimal.Decimal` support takes part in the `None`
  checks as before (`wilson_ci(None, Decimal(5))`, `wilson_ci(0.5, Decimal(0))` and
  `wilson_ci(0.5, Decimal("-Infinity"))` are `None`); past them it is a `TypeError` naming
  the Decimal, where the old Dojo raised `TypeError` from `float / Decimal` (from
  `Decimal(1)` up, integral or not) or `decimal.InvalidOperation` for a NaN Decimal. The
  corpus holds no NaN, infinite or Decimal support, so the ledger has no entry for them;
  `tests/test_statistics_faults.py` pins these behaviours.
- **An infinite `z` is refused** (it gave the interval `[0, 1]`).
- **Results are frozen dataclasses** (`BootstrapInterval`, `DeltaInterval`,
  `WilsonInterval`, `CrossFitResult`, `ReliabilityBin`, `PrecisionCutoff`); `to_dict()` is
  the old dictionary, key for key. Not a score change.

## Found, not changed (D1, statistics)

- **Scores are coerced as before.** `bootstrap_ci` and `delta_significance` drop `None` and
  anything `float()` cannot read (`"abc"`, `{}`), read booleans as 1.0 and 0.0 and numeric
  text (`" 0.5 "`) as numbers; `n` counts what was kept. An integer too large for a float
  raises `OverflowError`. The new paired functions are strict instead: numbers and booleans
  only.
- **`required_samples` names "confidence" for a bad `risk`** (`confidence must be in (0, 1);
  got 1.0`), because the old code validated both with one helper. The message is kept.
- **`wilson_ci` keeps `z` as given** (an integer `2` stays `2`) and rounds `p` to 4 places.
  The interval is computed with that `z`; `to_dict()` alone gives a NumPy or `Fraction` `z`
  as a plain `int` (an integer) or `float`, so the dictionary is JSON (the old one held the
  NumPy value). A NumPy `float32` or `float16` `z` still makes `lo`, `hi` and `half` NumPy
  values of that type, as in the old Dojo, so that dictionary is still not JSON.
- **The cross-fit shuffle and the bootstrap resampler are `random.Random(seed)`**, as in the
  old Dojo, so the same seed gives the same numbers across Python 3.12 and 3.13 (the corpus
  was captured with 3.12 and is replayed with 3.13).

## New (D1, statistics)

Not in the old Dojo, so not a difference: `paired_bootstrap_delta` (paired percentile
bootstrap of per-document score differences, B minus A), `outcome_flip_test` (exact
two-sided McNemar test on outcome flips; significant when `p < alpha`) and `calibration_by_group` (ECE, Brier and AUROC
per agent, class or both). These are the design's "Comparing runs" and "Confidence" needs.
The paired functions take `{document id: value}` mappings and pair by id, in sorted id
order; a document missing or `None` in one run is a `ValueError` naming it, unless the
caller passes `allow_unpaired=True`, which leaves it out and returns it in `unpaired_ids`.
`calibration_by_group` reads correctness strictly (a bool, NumPy's included, or `None`,
which is dropped and counted; text such as `"no"` or a number is a `TypeError` naming the
row) and reads NumPy confidences (`float32`, integers) as numbers. The ported ECE, Brier
and reliability functions keep the old lenient reading (`bool(correct)`, and only `int` or
`float` confidences), for parity.

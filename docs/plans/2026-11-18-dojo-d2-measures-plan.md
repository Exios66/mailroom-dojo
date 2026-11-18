# Plan: Dojo D2, `Measure` and the metric table

Date: 2026-11-18. Package: `packages/dojo` (`mailroom_dojo.measures`). Branch: `port/d2-measures`.
Chunk 2 of the workspace port design (`docs/plans/2026-11-18-workspace-port-design.md`, section 6):
"D2 headline registry metrics in one flat table, and `Measure`".

## Scope

- `Measure`: one number with its sample size, cost basis, label status, method (the metric id),
  subject (agent, document class, field), provenance (the old `PROVENANCE_KEYS` less
  `metric_id`), an optional interval and a reason when there is no value. Comparability is
  checked on method, subject, cost basis, label status and provenance.
- `metrics.yaml`: one row per id of the old registry (184, old order) plus the ten metrics the
  old golden baseline emits outside it (194 rows: 19 implemented, 93 planned, 82 not ported),
  each with direction, pairing, readers and status; each agent's default view; the direction
  overrides.
- The loader and its rules (`table.py`), with every rule tested and a deliberate mutation shown
  red for each.

Out of scope: computing any metric (D3b and later), the label assessment (D4), serving, cost and
completion (D5), failure causes (D6), comparison and the noise floor (D7).

## Decisions

1. **Values are data, rules are code.** Default views and direction overrides live in
   `metrics.yaml`; no agent or metric name is hard-coded in Python. The loader enforces the rules:
   the derivation order of a direction; a keep-rule row or a confirmed default view never has
   direction `none`; a `null` view needs a reason and is never confirmed; a confirmed view, or
   the view of an agent that has headline metrics, uses a headline metric; every agent with a
   kept headline metric has a view.
2. **Direction** (`derive_direction`, owner to confirm). In order: an explicit override; unit
   `error_rate` or an id containing `error`, `wrong`, `false_`, `violation`, `wasted`, `upper`,
   `ece`, `brier`, `latency`, `seconds`, `cost`, `usd`, `_p50` or `_p95` gives `lower`; an id
   containing `accuracy`, `f1`, `f2`, `precision`, `recall`, `completeness`, `similarity`,
   `coverage` or `per_second` gives `higher`; otherwise `none`. `none` is for descriptive behaviour
   rates and resource readings. Every value is frozen in
   `packages/dojo/tests/fixtures/measures/metric_directions.json`. No row matches both the lower
   and the higher words.
3. **Pairing.** Every kept headline or keep-rule row names a pairing or a `pairing_note`.
   `paired_percentile_bootstrap` is D1's `paired_bootstrap_delta` (per document, same documents
   on both sides); `two_sample_percentile_bootstrap` is D1's `delta_significance`. `f1_macro`,
   `content_topic_f1_macro` and `reviewer_wrong_accept_upper_95` are whole-set statistics, so a
   per-document pairing does not apply; their notes say so and D7 decides their comparison.
4. **Noise floor deferred to D7.** The keep rule's minimum meaningful difference per metric is a
   comparison setting and lands with D7 in `config/`, not in this table.
5. **Aliases.** `contracteval_false_no_related` is an alias of `false_no_related_rate` (same
   unit, direction and aggregation; `not_ported`); `MetricTable.resolve()` follows it and
   `check()` refuses a `Measure` filed under the alias.
6. **`n == 0` gives no value.** New in D2 (see the score changelog): a measure over nothing is
   missing with a reason, never `0.0`. D3b applies it to the ported scorers.
7. **`composite`** (the chained summary's weighted score, `tasks.chained_summary`) is a planned
   row for D3b's golden parity gate.

## Default views (provisional; the owner is reviewing them)

Confirmed only where the old registry names the number; everything else is a proposal.
`judge_verdict_accuracy` is the alternative the owner may prefer for `judge`. The auditors'
`audit_resolution_rate` has direction `none`: a stopgap until D3b proposes a labelled measure
("net fields corrected by audit"). Sources per entry are in `metrics.yaml` and frozen in
`packages/dojo/tests/fixtures/measures/default_views.json`.

| Agent | Default view | Status | Secondary or guard |
|:---|:---|:---|:---|
| `sorter` | `f1_macro` | confirmed |  |
| `judge` | `f1_macro` | confirmed |  |
| `contracts_specialist` | `extraction_overall_score` | confirmed |  |
| `merger_agreement_specialist` | `extraction_overall_score` | confirmed |  |
| `corporate_records_specialist` | `extraction_overall_score` | confirmed |  |
| `due_diligence_specialist` | `extraction_overall_score` | confirmed |  |
| `compliance_specialist` | `extraction_overall_score` | confirmed |  |
| `court_opinions_specialist` | `extraction_overall_score` | confirmed |  |
| `insurance_claims_specialist` | `extraction_f2` | confirmed |  |
| `correspondence_specialist` | `content_topic_f1_macro` | confirmed |  |
| `reporter` | `extraction_overall_score` | proposal |  |
| `boss` | `extraction_overall_score` | proposal |  |
| `sorter_reviewer` | `reviewer_class_accuracy` | proposal | `reviewer_wrong_accept_upper_95` |
| `local_vs_api` | `tokens_per_second` | proposal | `ttft_seconds` |
| `arbiter` | `arbiter_decision_accuracy` | proposal |  |
| `audit_agent` | `audit_resolution_rate` | proposal |  |
| `contract_auditor` | `audit_resolution_rate` | proposal |  |
| `corporate_records_auditor` | `audit_resolution_rate` | proposal |  |
| `due_diligence_auditor` | `audit_resolution_rate` | proposal |  |
| `correspondence_auditor` | `audit_resolution_rate` | proposal |  |
| `compliance_auditor` | `audit_resolution_rate` | proposal |  |
| `court_opinions_auditor` | `audit_resolution_rate` | proposal |  |
| `insurance_claims_auditor` | `audit_resolution_rate` | proposal |  |
| `pdf_transcriber` | `word_accuracy` | proposal |  |
| `image_extractor` | `word_accuracy` | proposal |  |
| `intake` | `intake_prep_completeness` | proposal |  |
| `human_review` | `review_tray_wasted_review_rate` | proposal |  |
| `archivist` | none (procedural; judged by archive-gate checks, not a metric) | proposal |  |

## Direction overrides (derived, owner to confirm)

Overrides set a direction the name does not show:

- **higher (27):** `amount_exactness`, `arbiter_retry_usefulness`, `bert_sorter_agreement`, `class_correct`, `classification_correct`, `classification_quality`, `composite`, `determination_consistency`, `exact_match`, `expected_field_presence`, `extraction_category_presence`, `extraction_correctness`, `extraction_field_score`, `extraction_overall_score`, `field_presence`, `jaccard_median`, `mailroom-pipeline-judge`, `mailroom-pipeline-quality`, `maud_clause_presence`, `maud_valid_class_rate`, `parse_ok`, `per_field_scores`, `schema_adherence`, `schema_valid`, `stage_completed`, `stage_correct`, `success_rate`.
- **lower (9):** `bert_elapsed_ms`, `boss_needless_review_rate`, `date_mae_days`, `duration_mae_days`, `extraction_hallucination_rate`, `hallucination_rate`, `laziness_rate`, `ml_intake_selective_risk`, `run_aborted`.
- **none (16):** `audit_disagreement_rate`, `audit_resolution_rate`, `completeness_label`, `completion_tokens`, `gpu_memory_used_gb`, `gpu_utilization`, `intake_changed_rate`, `intake_collapsed_blanks`, `intake_hyphen_unwraps`, `intake_messy_rate`, `kv_cache_utilization`, `prompt_tokens`, `review_tray_correction_rate`, `review_tray_reject_rate`, `reviewer_routed_to_human_review_rate`, `serving_kind`.

Rows with direction none: 51, of which 18 are kept (implemented or planned): `reviewer_routed_to_human_review_rate`, `audit_disagreement_rate`, `audit_resolution_rate`, `intake_changed_rate`, `intake_messy_rate`, `intake_hyphen_unwraps`, `intake_collapsed_blanks`, `gpu_utilization`, `prompt_tokens`, `completion_tokens`, `confusion_matrix`, `per_class_stats`, `bootstrap_ci`, `llm_call_count`, `total_tokens`, `classification_attempts`, `extraction_attempts`, `no_related_rate`. The other 33 are `not_ported`.

## The old `scorecard_honesty.py`

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

## Review findings, round 1

| Finding | Change |
|:---|:---|
| 1 Default views (blocking) | `default_views` in `metrics.yaml`, validated by the loader; fixture and tests |
| 2 Port design edited | Restored; this plan holds D2 |
| 3 `scorecard_honesty` disposition | The tables above, repeated in the score changelog; `measure.py` docstring narrowed |
| 4 Subject and provenance | `Subject`, `Provenance`, `comparability_problems(..., vary=...)` |
| 5 Pairing, noise floor | `pairing` or `pairing_note` on every kept headline or keep-rule row; noise floor to D7 |
| 6 Directions | Rule in code, overrides in data, all values in a fixture; name-pattern tests |
| 7 Capture | Nested golden blocks (28) and `chained_summary` captured; fixture re-captured byte for byte |
| 8 Plan and test strategy | This file; D2 section in `docs/test-strategy-dojo-core.md` |
| 9 `n == 0` | Score changelog entry (D3b applies it), and the earlier commit wording corrected there |
| 10 Alias | `alias_of` column; `contracteval_false_no_related` |
| 11 `check()` | Whole counts, interval in the unit range and around the value, `missing()` unverified by default, integer `n` of any integral type |
| 12 Serving ids | Left `planned` for D5 |

Mutation evidence: `docs/test-strategy-dojo-core.md`, "D2".

# Plan: Dojo D2, `Measure` and the metric table

Date: 2026-11-18. Package: `packages/dojo` (`mailroom_dojo.measures`). Branch: `port/d2-measures`.
Chunk 2 of the workspace port design (`docs/plans/2026-11-18-workspace-port-design.md`, section 6):
"D2 headline registry metrics in one flat table, and `Measure`". Gate: every old registry id and
every metric the old `scores.json` emits is in the table with the same meaning (or an intended
change), and every live agent has a `default_view` or a null with a reason.

## Scope

- `Measure`: one number with its sample size, cost basis, label status, method (the metric id),
  subject (agent, document class, field), provenance (the old `PROVENANCE_KEYS` less
  `metric_id`), an optional interval and a reason when there is no value. Comparability is
  checked on method, subject, cost basis, label status and provenance.
- `metrics.yaml`: one row per id of the old registry (184, old order), the ten metrics the old
  golden baseline emits outside it, and 27 planned rows for needs the master design names that
  no old metric meets (eight of them, `<old id>_vs_gold`, carry the decision agents' derived
  gold beside the unchanged old ids): 221 rows (19 implemented, 116 planned, 86 not ported). Each row has a
  direction, readers, status, pairing where it is compared, and a pointer to its canonical row
  when it is a duplicate. An `agent_roster` and each live agent's default view.
- The loader and its rules (`table.py`), each tested, with a deliberate mutation shown red.

Out of scope: computing any metric (D3b and later), the label assessment (D4), serving, cost and
completion (D5), outcomes, behaviour and the archive gate (D6), comparison and the noise floor
(D7), the transcribers (deferred by the owner) and generating additional gold (a paid run
behind the spend gate).

## Decisions

1. **Values are data, rules are code.** Default views, the roster and every direction live in
   `metrics.yaml`; no agent or metric name is hard-coded in Python. The loader enforces the
   rules below.
2. **Direction is stated, not derived.** The `direction` column is the source of truth
   (higher, lower or none; none for descriptive rates and readings). The old registry stated
   none, so every value is proposed for the owner to confirm and frozen in
   `packages/dojo/tests/fixtures/measures/metric_directions.json`. A test lints each direction
   against the id's whole words (split on `_` and `-`, so `ece` never matches inside
   `unnecessary`) and fails only on a disagreement that is not one of the reviewed
   `name_rule_exceptions`, or on a stale exception. A keep-rule row, a default view and a guard
   never have direction none.
3. **Default views apply to live agents.** `agent_roster` lists every agent with a status:
   `live` (the 16 agents of llm-mailroom `src/config/taxonomy.yaml`), `legacy` (the eight
   auditors), `retired` (the due-diligence, compliance and court-opinions specialists),
   `procedural`, `human`, `comparison` and `graph_node`. Every live agent has a view entry; a
   legacy or retired agent has none. A view names its metric and an optional guard (both kept,
   applying to the agent, with a direction) and the label layers its gold comes from; a `null`
   view gives a reason. `confirmed` marks the owner's decisions; the rest are proposals.
4. **Gold sources (owner decision, 2026-11-18).** Gold for evaluation is the dataset ground
   truth (`hub`), the Claude interim and near-gold labels already generated (`interim`,
   `near_gold`), the review desk's gold (`review`), and further gold generated to support the
   evaluations (a planned paid run behind the spend gate); it no longer waits for human
   resolutions. Label precedence does not change: the labels package's profile orders the ranks
   as before. Each view names its layers (the labels package's layer kinds `hub`, `interim`,
   `near_gold` and `review`, or `derived_gold` alone; `derived` is not used, because the labels
   package already uses it for an interim truth status). The decision agents (sorter reviewer,
   judge, arbiter, boss) read `derived_gold`, computed deterministically from the class and
   field labels in those layers, with the score bands taken from the scoring profile in
   `config/`. Derived gold is a new rule, and every kept id keeps its old meaning (port design,
   section 10, decision 4), so the rule lives in eight new design rows named
   `<old id>_vs_gold`, each stating it in `status_reason`; the old ids keep their old rule
   (human outcome or verified target first). The loader refuses a view on `derived_gold` that
   shows a kept old row. No scoring code for it in D2.
   The planned row `fields_without_gold` is what the future gold-generation step reads.
5. **Pairing.** Every kept headline or keep-rule row, and every metric a default view shows
   (its number or its guard), names a pairing or a `pairing_note`.
   `paired_percentile_bootstrap` is D1's `paired_bootstrap_delta` (per document, same documents
   on both sides); `two_sample_percentile_bootstrap` is D1's `delta_significance`. `f1_macro`,
   `content_topic_f1_macro`, `extraction_f1`, `extraction_f2` and
   `reviewer_wrong_accept_upper_95` are whole-set statistics (macro F1; field-micro F1 and F2
   pool the counts of every document before dividing, old `suites.py` line 1486
   `merge_extraction_counts`; a bound), so their notes say why there is no per-document
   pairing; D7 decides their comparison. A `denominator` column says what sets a row's
   denominator: `gold` (the evaluation set, the same documents for every run) or `run` (the
   run's own decisions: the documents that reached a lane, its rulings, accepts or archives,
   or a verdict read from its own extraction). The loader refuses any pairing on a `run` row
   and requires `gold` on every `paired_percentile_bootstrap` row. The `run` rows that are
   compared carry a `pairing_note` instead, and D7 builds a resample that recomputes the share
   on both runs: the arbiter's and boss's rates (shares of their rulings), the sorter
   reviewer's (the sorter decides which documents reach it), the judge's verdict accuracy (the
   documents routed to the judge, without its `judge_error` rows) and escalation recall (over
   documents whose derived verdict is read from the run's own extraction), and
   `wrong_archive_rate` (over the run's own scorable archives). The paired rows divide by
   gold: `accuracy`, `extraction_overall_score`, `reviewer_accept_rule_violations` (a count
   over every document), `cost_per_document`, `per_field_scores`, `review_rate`,
   `document_utility` and `intake_class_accuracy`.
6. **Noise floor deferred to D7.** The keep rule's smallest meaningful difference is measured by
   repeat runs and lands with D7 in `config/`, not in this table.
7. **Duplicates point at the canonical row.** `alias_of` (a not-ported second name, same unit,
   direction and aggregation) and `same_as` (a duplicate that keeps its own row, same unit and
   direction): `contracteval_false_no_related` and `laziness_rate` point at
   `false_no_related_rate` (the old note "mirrors no_related_rate" was wrong);
   `schema_adherence` at `schema_valid`; `expected_field_presence` at `field_presence`.
   `audit_disagreement_rate` is `1 - audit_resolution_rate`, said in its `status_reason`.
8. **One spelling per unit.** The unit vocabulary is closed: `usd` is `USD`, `int` is `count`,
   and `confusion_matrix`, `per_class_stats` and `bootstrap_ci` are `structure` (not a number).
   A `structure`, `tag` or `text` row has direction none and no pairing, and is never a default
   view or guard (loader rules).
   New units for the design rows: `per_1000`, `docs/min`, `ratio`, and `utility` (the only unit
   that may be negative).
9. **`n == 0` gives no value.** New in D2 (see the score changelog): a measure over nothing is
   missing with a reason, never `0.0`. D3b applies it to the ported scorers.
10. **Intake clerk diagnostics dropped (owner decision).** `intake_prep_completeness`,
    `intake_changed_rate`, `intake_messy_rate`, `intake_hyphen_unwraps` and
    `intake_collapsed_blanks` are `not_ported`; the old golden intake suite emits them, so
    D3b lists them as intended parity differences. Intake's default view is the planned design
    row `intake_class_accuracy` (its triage class against the gold class, direction higher,
    unit proportion, gold from the class layers, no guard), not `accuracy`: the shared ids keep
    their old meaning, and in the old intake suite `accuracy` is the exact match of the cleaned
    text against the clerk's gold and `f1_macro` the token F1 (old `pipeline/intake.py`
    `_score_one`). The old suite's numbers stay as they are for the parity gate.
11. **`judge_error_rate` is kept** for the archive gate, whose pass rule requires no
    `judge_error` document (local-mailroom-sandbox `job/gate.py`). Serving ids stay planned
    for D5.
12. **Six golden values are intended D3b parity differences.** `gpu_memory_used_gb`,
    `kv_cache_utilization`, `queue_time_seconds` and `prompt_tokens_per_second` (serving
    telemetry the new engine does not emit, in `suites.local_vs_api.score.{api,local}`) and
    `false_negative_rate` and `false_positive_rate` (LegalBench binary error rates with no
    reader, in `task_kinds.legalbench.score.binary`) stay `not_ported`; the score changelog
    lists them and a test checks that every row a per-side or binary block names is kept or
    listed.

## Default views (provisional where not confirmed)

| Agent | Status | Default view | Guard | Gold from | Confirmed |
|:---|:---|:---|:---|:---|:---|
| `sorter` | live | `f1_macro` | `accuracy` | hub, interim, near_gold, review | yes |
| `sorter_reviewer` | live | `reviewer_class_accuracy_vs_gold` | `reviewer_wrong_accept_upper_95_vs_gold` | derived_gold | proposal |
| `judge` | live | `judge_verdict_accuracy_vs_gold` | `judge_escalation_recall_vs_gold` | derived_gold | proposal |
| `arbiter` | live | `arbiter_decision_accuracy_vs_gold` | `arbiter_wrongful_accept_rate_vs_gold` | derived_gold | proposal |
| `boss` | live | `boss_wrongful_approve_rate_vs_gold` | `boss_needless_review_rate_vs_gold` | derived_gold | yes (guard proposal) |
| `intake` | live | `intake_class_accuracy` | none | hub, interim, near_gold, review | yes |
| `contracts_specialist` | live | `extraction_overall_score` | `per_field_scores` | hub, interim, near_gold, review | proposal |
| `merger_agreement_specialist` | live | `extraction_overall_score` | `per_field_scores` | hub, interim, near_gold, review | proposal |
| `corporate_records_specialist` | live | `extraction_overall_score` | `per_field_scores` | hub, interim, near_gold, review | proposal |
| `insurance_claims_specialist` | live | `extraction_f2` | `per_field_scores` | hub, interim, near_gold, review | proposal |
| `correspondence_specialist` | live | `extraction_overall_score` | `per_field_scores` | hub, interim, near_gold, review | proposal |
| `local_vs_api` | comparison | `tokens_per_second` | `cost_per_document` | no labels | proposal |
| `pdf_transcriber` | live | none: deferred by owner decision | | | yes (owner) |
| `image_extractor` | live | none: deferred by owner decision | | | yes (owner) |
| `reporter` | live | none: procedural; no model decision to score | | | proposal |
| `archivist` | procedural | none: procedural; no model decision to score | | | proposal |
| `human_review` | human | none: humans are the gold source | | | yes (owner) |
| `relations` | live | none: no metric yet: the old registry has none for relations and no reader needs one | | | proposal |
| `gmail_triage` | live | none: no metric yet: mail intake ahead of the pipeline; the old registry has none and no reader needs one | | | proposal |
| `classification_arbiter` | graph_node | none: the graph node of the arbiter agent; its decisions are scored under arbiter | | | proposal |

No default view (rows kept for golden parity and history): legacy `audit_agent`, `contract_auditor`, `corporate_records_auditor`, `due_diligence_auditor`, `correspondence_auditor`, `compliance_auditor`, `court_opinions_auditor`, `insurance_claims_auditor`; retired `due_diligence_specialist`, `compliance_specialist`, `court_opinions_specialist`.

Notes on the proposals: `judge_verdict_accuracy_vs_gold` replaces `f1_macro` for the judge, which
has no class gold in the new system. The correspondence default is `extraction_overall_score`, not
`content_topic_f1_macro`: the live correspondence schema has no `content_topic` field, so a
perfect extraction would score 0.0; that row stays for eval-lineage runs. `local_vs_api` is a
serving comparison kept for continuity; the backend is a column of the run, not an agent.
Wasted review is attributed to the routing policy (`review_tray_wasted_review_rate`), not to
`human_review`.

## Planned design rows

`silent_wrong_per_1000`, `wrong_archive_rate`, `wrong_archive_upper95` (Wilson 95% upper bound,
the sandbox gate's name), `review_rate`, `correct_archive_rate`, `document_utility`,
`slot_override_rate`, `slot_cap_use`, `slot_cap_breaches`, `cost_per_decision`,
`outcome_flip_rate`, `cost_per_classification`, `cost_per_1000_documents`,
`documents_per_minute`, `log_complete_step_rate`, `log_chains_verified`,
`cost_estimate_to_billed_ratio`, `fields_without_gold` and `intake_class_accuracy`; and the eight
rows that carry the decision agents' derived gold beside the unchanged old ids:
`reviewer_class_accuracy_vs_gold`, `reviewer_wrong_accept_upper_95_vs_gold`,
`judge_verdict_accuracy_vs_gold`, `judge_escalation_recall_vs_gold`,
`arbiter_decision_accuracy_vs_gold`, `arbiter_wrongful_accept_rate_vs_gold`,
`boss_wrongful_approve_rate_vs_gold` and `boss_needless_review_rate_vs_gold` (same unit,
direction, aggregation and agents as the old row; description, rule and citation their own).
Each is `origin: design`, metric id `dojo:<id>`, and names its reader (archive gate, reports,
keep rule, run comparison, cost projection, log health or gold generation) and its build chunk
(D4 to D7).

## Directions (proposed, owner to confirm)

Directions: 105 higher, 62 lower, 54 none, over 221 rows. The 17 kept rows with direction none: `reviewer_routed_to_human_review_rate`, `audit_disagreement_rate`, `audit_resolution_rate`, `gpu_utilization`, `prompt_tokens`, `completion_tokens`, `confusion_matrix`, `per_class_stats`, `bootstrap_ci`, `llm_call_count`, `total_tokens`, `classification_attempts`, `extraction_attempts`, `no_related_rate`, `slot_override_rate`, `slot_cap_use`, `cost_estimate_to_billed_ratio`.

Name-lint exceptions (49), each a row whose direction differs from what its id's words suggest:

- **higher (32):** `extraction_overall_score`, `arbiter_retry_usefulness`, `field_presence`, `determination_consistency`, `amount_exactness`, `schema_valid`, `parse_ok`, `schema_adherence`, `success_rate`, `classification_correct`, `class_correct`, `stage_correct`, `extraction_correctness`, `expected_field_presence`, `mailroom-pipeline-judge`, `mailroom-pipeline-quality`, `maud_clause_presence`, `maud_valid_class_rate`, `per_field_scores`, `extraction_field_score`, `extraction_category_presence`, `classification_quality`, `stage_completed`, `bert_sorter_agreement`, `exact_match`, `jaccard_median`, `composite`, `correct_archive_rate`, `document_utility`, `documents_per_minute`, `log_complete_step_rate`, `log_chains_verified`.
- **lower (15):** `ml_intake_selective_risk`, `arbiter_unnecessary_escalation_rate`, `boss_needless_review_rate`, `boss_needless_review_rate_vs_gold`, `laziness_rate`, `extraction_hallucination_rate`, `date_mae_days`, `duration_mae_days`, `hallucination_rate`, `run_aborted`, `bert_elapsed_ms`, `review_rate`, `slot_cap_breaches`, `outcome_flip_rate`, `fields_without_gold`.
- **none (2):** `completeness_label`, `cost_estimate_to_billed_ratio`.

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

## Review findings

Round 1:

| Finding | Change |
|:---|:---|
| 1 Default views (blocking) | `default_views` in `metrics.yaml`, validated by the loader; fixture and tests |
| 2 Port design edited | Restored; this plan holds D2 |
| 3 `scorecard_honesty` disposition | The tables above, repeated in the score changelog; `measure.py` docstring narrowed |
| 4 Subject and provenance | `Subject`, `Provenance`, `comparability_problems(..., vary=...)` |
| 5 Pairing, noise floor | `pairing` or `pairing_note` on every kept headline or keep-rule row; noise floor to D7 |
| 6 Directions | Every value in a fixture; name-pattern tests |
| 7 Capture | Nested golden blocks (28) and `chained_summary` captured; fixture re-captured byte for byte |
| 8 Plan and test strategy | This file; D2 section in `docs/test-strategy-dojo-core.md` |
| 9 `n == 0` | Score changelog entry (D3b applies it), and the earlier commit wording corrected there |
| 10 Alias | `alias_of` column; `contracteval_false_no_related` |
| 11 `check()` | Whole counts, interval in the unit range and around the value, `missing()` unverified by default, integer `n` of any integral type |
| 12 Serving ids | Left `planned` for D5 |

Review of the provisional defaults, and the owner's decisions:

| Point | Change |
|:---|:---|
| Direction derived from names | The column is the source of truth; the name rule is a whole-word lint with reviewed exceptions (49 now) |
| Defaults for non-live agents | `agent_roster`; auditors legacy, three specialists retired, no views for them |
| Per-agent defaults and guards | The table above; boss and intake per the owner |
| Gold sources | `label_layers` on every view; derived-gold rules in the decision rows |
| Design needs without rows | 19 planned `design` rows with readers (27 after round 3) |
| `judge_error_rate` "no reader" | Planned for the archive gate |
| Units and duplicates | Closed unit vocabulary; `same_as` and `alias_of` pointers |
| Port design and plan | Section 4.2, section 6 row 2 and section 10 updated; this plan |

Mutation evidence: `docs/test-strategy-dojo-core.md`, "D2".

# mailroom-dojo

The `mailroom_dojo` package: the pure scoring layers of the Mailroom Dojo, driven by a
per-client **scoring profile**. It decides what "correct" means for one extracted field,
one label or one document class. It holds no global state, reads no environment
variable or clock, and opens no network connection. It depends on no other workspace
package today (it may import `mailroom_record` and, once it lands, `mailroom_labels`; the
label interface is structural, so no import is needed) and nothing from any engine or
from `llm_dojo_scoring`.

```bash
.venv/bin/pip install -e "packages/dojo[dev]"
.venv/bin/python -m pytest packages/dojo/tests -q -n 4
```

## Layers

| Module | What it does |
|:---|:---|
| `profile/` | `load_scoring_profile(path)` reads, validates and freezes a scoring profile: classes, field types, label vocabularies, row variants, thresholds. A malformed profile raises `ProfileError` naming the YAML path. A profile is immutable, hashable and identified by `content_hash`. |
| `profile/label_folding.py` | `LabelVocabulary`: a closed set of label tokens and how written labels fold onto them (`exact`, `aliases`, `inventory`, `catalog`, `allowed_set`). |
| `values/` | `is_empty_value`, `parse_json_container`: what counts as absent. |
| `normalization/` | text (`normalize_text`, token similarities, Jaro-Winkler), money (`parse_money`) and dates (`parse_date_parts`, with a fixed two-digit-year pivot, never the clock). |
| `field_scoring/scalar_scorers.py` | one scorer per scalar type: `id`, `date`, `money`, `name`, `label`, `free_text`, `containment`. Each is `(predicted, expected, rescue=None) -> float` in `[0, 1]`. |
| `field_scoring/scorer_registry.py` | `FieldScorerRegistry`: type name → scorer, built-ins plus the plug-ins a profile declares; `resolve("entity_list:name")`. |
| `field_scoring/plugins/` | plug-in scorers bundled with the Dojo (`cms_doc_ref` for CMS supporting-document identifiers). |
| `field_scoring/similarity_backend.py` | optional embedding rescue: an injected `SimilarityBackend`, with every backend failure kept as a reported `SimilarityFallback`. Off unless a caller passes a backend. |
| `field_scoring/sentence_transformer_backend.py` | an optional local `sentence-transformers` backend (extra `embeddings`, local files only). |
| `field_scoring/entity_lists.py` | `score_entity_list`: pairs items by the Hungarian algorithm (scipy), thresholded by `thresholds.list_match_threshold`, then precision, recall and F1. The greedy matcher runs only when asked (`matcher="greedy"`) or allowed as the fallback for a missing scipy (`fallback_matcher="greedy"`); `EntityListScore.matcher` and `.matcher_fallback` say which ran and why. |
| `field_scoring/audits.py` | the factuality audit: `audit_list_field`, `audit_scalar_field` (a predicted value is true when it matches the label or is grounded in the document text; dates are grounded in any format), `verify_list_items`, `field_audit_support`. |
| `field_scoring/category_presence.py` | `score_category_presence`: the share of labelled clause categories a predicted span covers, with reasoning-trace routing and an optional `PresenceSimilarity` whose backend failures are reported. |
| `field_scoring/row_variants.py` | the rules of a profile's row variants (documented-absent fields, type overrides, an unstated marker, a marker read from a fallback field). |
| `field_scoring/trace_capture.py` | `capture_trace`: model-emitted `confidence` and `reasoning`, captured for the run log under the profile's `TraceCaptureOptions`, never scored. |
| `field_scoring/extraction.py` | `score_extraction(profile, class_key, predicted, expected, ...)` → `ExtractionScoreResult` (field scores, overall score, judge-review fields, list scores, audits, trace). A plug-in scorer that returns anything but a finite number in `[0, 1]` raises `ScorerResultError`. |
| `classification/` | `ClassLabelSet.from_profile(profile)` and the classification metrics: `normalize_label`, `exact_match`, `accuracy`, `fbeta`, `per_class_stats`, `macro_accuracy`, `macro_prf`, `confusion_matrix`, `top_confusions`, `binary_metrics`, `class_distribution`. The class set, its match order and retired classes come from the profile. |
| `statistics/` | `bootstrap_ci`, `delta_significance` (unpaired), `wilson_ci`, `wilson_bounds`; calibration and risk: `clopper_pearson_upper`, `required_samples`, `cutoff_at_target`, `cross_fit_cutoffs`, `cutoff_for_precision`, `expected_calibration_error`, `brier_score`, `reliability_bins`, `auroc` (ranking power) and `spearman`. Seeds are arguments; results are frozen dataclasses whose `to_dict()` is the old Dojo's dictionary. |
| `labels_interface/` | structural Protocols (`LabelSource`, `ExpectedDocument`, `PresenceExpectation`) that the labels package satisfies, `expected_values(...)` (keeps scorable fields only; refuses or flags unverified documents), and `hub_rows.py`, a reference adapter for Hub rows that is meant to move to `mailroom_labels`. |

## The scoring profile

`config/clients/mailroom-dataset/scoring.yaml` is the profile for the mailroom dataset.
It reproduces the old Dojo (llm-dojo-scoring 0.30.1, commit `23f8234c`) exactly: eight
classes in the old registry order (three retired), their field types, the intent,
subclass and label vocabularies, the CMS DE-SynPUF row variant and every default
threshold.

## Adding a document type or a client

Nothing in Python changes for a new document type built from existing field types:

1. Add a class under `classes:` in the client's `scoring.yaml` (or a new
   `config/clients/<client>/scoring.yaml` with `profile_format: 1`, `client`, `version`).
2. List its fields with a type: `id`, `date`, `money`, `name`, `label`, `free_text`,
   `containment`, `entity_list` or `entity_list:<scalar type>`. A `label` field needs a
   `vocabulary` (`tokens`, a `fold` method and, for `aliases`/`inventory`/`catalog`, an
   alias table).
3. Optional: `compare: containment` for location-like text, `coverage: recall` for lists
   whose label may be partial, `subclasses` with a `catalog` or `allowed_set` fold, and
   `variants` for rows with a different shape inside one class.
4. Load it: the loader refuses unknown keys, unregistered types, label fields without a
   vocabulary, duplicate classes and out-of-range thresholds, naming the YAML path.

A field type the built-ins do not cover (a VIN, a policy number format) is a **plug-in**:
write a scorer `(predicted, expected, rescue=None) -> float`, declare its name under
`scorer_plugins:` in the profile, and pass it in code with
`FieldScorerRegistry.for_profile(profile, {"vin": score_vin})`. A plug-in cannot replace
a built-in type, and a plug-in passed in code that the profile does not declare is refused.

## Embedding rescue

Scoring is string-only by default. To let a semantic backend lift low `name` and
`free_text` scores, set `embedding.enabled: true` in the profile and pass a backend:

```python
rescue = EmbeddingRescue.from_profile(profile, SentenceTransformerBackend("/models/minilm"))
score_name_field("ACME Corp", "Acme Corporation of Delaware", rescue)
rescue.fallbacks  # every time the backend raised, returned NaN, a non-number or a value outside [0, 1]
```

## Scoring an extraction

```python
result = score_extraction(profile, "insurance_claim", predicted, expected, doc_text=document_text)
result.to_dict()                 # field_scores, overall_score, ambiguous_fields, entity_list_scores, entity_list_audit, trace
result.audit_support             # audited fields, mean verified precision and hallucination rate
result.similarity_fallbacks      # every time an embedding backend's answer was not used
result.field_type_fallbacks      # unregistered types scored as name (only with unknown_field_types="score_as_name")
```

With `field_types=None` the class's field types come from the profile; an explicit map
is used as given (it also opts retired and accepted-unscored keys back in). An
unregistered type in a caller's map raises `UnknownFieldTypeError`; an expected key with
no type follows the profile's `unmapped_fields` policy. Pass `apply_variants=False` when
the expected values are not ground truth (comparing two model outputs).

## Tests

See `docs/test-strategy-dojo-core.md`. The differential corpus
(`tests/fixtures/differential/scalar_cases.jsonl`) holds 21,009 inputs with the old Dojo's
outputs; `scripts/capture_old_dojo_scalar_cases.py` regenerates it from an old checkout
and never runs in CI.

The extraction corpus (`tests/fixtures/differential/extraction_cases.jsonl`, 6,414 cases)
holds the old golden run's `score_extraction` calls, tied to the old `scores.json` by
path and hash, the calls the old tests make into the extraction layers, and seeded
generated inputs, many under non-default settings that the replay builds into a profile.
`scripts/capture_old_dojo_extraction_cases.py` regenerates it byte for byte.

The statistics corpus (`tests/fixtures/differential/statistics_cases.jsonl`, 1,979 cases)
holds every call the old golden run and eleven old test files made into the old bootstrap
and calibration modules, plus seeded inputs. Every valid input gives the old number; the
209 invalid inputs that now raise are listed with reasons in
`statistics_intended_differences.json` and in `docs/score-changelog.md`.
`scripts/capture_old_dojo_statistics_cases.py` regenerates the corpus byte for byte.

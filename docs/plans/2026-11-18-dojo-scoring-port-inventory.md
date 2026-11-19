# Dojo port inventory: field scoring, classification, goldens

Read-only investigation, 2026-11-18. Old Dojo: `<old-dojo-checkout>` at
`23f8234c3c548bdc203033388bb316e52e588a2c` (version 0.30.1). New target:
`<repo>/packages/dojo/src/mailroom_dojo/`. Nothing in either repo was
changed. Two of my output files briefly landed in the old repo's root and were moved out
at once; `git status --short` there is empty.

Scratch scripts (all in this scratchpad, re-runnable):

| Script | What it does |
| --- | --- |
| `closure.py` | AST import closure inside `llm_dojo_scoring` (top-level vs lazy imports) |
| `trace_goldens.py` | `sys.setprofile` over `freeze_goldens` collectors: which in-scope functions each golden case reaches, and through which out-of-scope caller |
| `capture_boundary.py` | records every call that crosses INTO the in-scope modules during the golden run (args, return): `boundary_calls.json`, 704 calls |
| `digest_se.py` | checks the 16 captured `score_extraction` calls against `scores.json` and the registry |
| `hardcodes.py` | AST scan for class and field string literals in the in-scope files |
| `testuse.py` | which old test files call in-scope names directly |
| `cov_golden.py` + coverage | line and branch coverage of the in-scope modules from the golden run alone |

How the old Dojo was run without touching it (it works; `freeze_goldens.py --check` exits 0):

```bash
SP=<scratchpad>
uv venv -q --python 3.12 $SP/olddojo-venv
VIRTUAL_ENV=$SP/olddojo-venv uv pip install -q packaging pandas numpy PyYAML python-dateutil \
  openpyxl matplotlib scipy structlog jellyfish pytest pytest-mock jsonschema pydantic coverage
cd <old-dojo-checkout>
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=$PWD $SP/olddojo-venv/bin/python scripts/freeze_goldens.py --check   # exit 0
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=$PWD $SP/olddojo-venv/bin/python -m pytest -q -p no:cacheprovider tests/test_field_scoring.py ...
```

No editable install, no bytecode, no pytest cache, so the old repo stays clean. Write every
output to an absolute scratchpad path (a relative path lands in the old repo).

---

## 1. Import closure

`field_scoring.py` and `classification.py` reach these modules. "Lazy" means the import
is inside a function. **Runtime caveat:** importing any `llm_dojo_scoring.*` submodule runs
`llm_dojo_scoring/__init__.py`, which imports the whole package (metrics, suites,
pipeline, ...). So the real runtime closure is the whole Dojo. The table is the
*logical* closure: what the code actually uses.

| Module | LOC | Role | Verdict |
| --- | ---: | --- | --- |
| `scoring/field_scoring.py` | 1957 | scalar scorers, entity lists (Hungarian), audits, presence, embedding rescue, `score_extraction` | **PORT AND DE-HARDCODE** (split into several modules, see section 4) |
| `scoring/classification.py` | 321 | `normalize_label`, `exact_match`, `accuracy`, `per_class_stats`, `confusion_matrix`, `binary_metrics`, `macro_prf`, `fbeta`, ... | **PORT AND DE-HARDCODE** (class regex table, retired list) |
| `scoring/_paired.py` | 9 | `require_equal_lengths` | **PORT AS IS** |
| `scoring/field_support.py` | 48 | `field_audit_support` (lazy, from `ExtractionScoreResult.audit_support`); the other two functions are pilot/schema helpers | **PORT** `field_audit_support` only; drop `pilot_ground_truth_scores` and `schema_gt_support` (callers are out of scope) |
| `scoring/trace_knobs.py` | 239 | `capture_trace_knobs`: peels `confidence` and `reasoning` off a prediction. Its output is `ExtractionScoreResult.trace`, which is inside `to_dict()` and so inside the golden numbers | **PORT AND DE-GLOBALIZE** (settings become an explicit `TraceCaptureOptions` argument) |
| `schemas/gt_metadata.py` | 344 | `parse_json_container`, `is_empty_value` (generic); `ANNOTATION_KEYS`; Hub `gt_presence` shaping (`scoring_gt_fields`, `parse_gt_fields`, `gt_presence_map`); CUAD presence expectations | **SPLIT.** Value helpers: PORT AS IS. `ANNOTATION_KEYS`: becomes profile data. Hub shaping and presence-expectation builders: **NEEDS LABELS-PACKAGE INTERFACE** |
| `schemas/scored_enums.py` | 268 | `canonical_label` (label folding), `SCORED_ENUM_FIELDS` vocabularies, prompt-hint helpers | `canonical_label` folding: **PORT AND DE-HARDCODE** (vocabularies and alias tables become profile data). Hint and schema helpers: **DROP** here (prompts move to the engine) |
| `schemas/intents.py` | 224 | intent vocabularies, `INTENT_ALIASES`, the `_normalize` inventory-folding algorithm, `normalize_intent` (with a merger `amend` special case) | Algorithm: **PORT** as the generic "inventory fold". Vocabularies, aliases, descriptions: **profile data**. The `amend` rule becomes a profile `prefix_aliases` entry |
| `schemas/corpus.py` | 525 | needs only: `CMS_SOURCE_TABLES`, `CMS_DOCUMENTED_ABSENT_FIELDS`, `normalize_corpus_subclass` (via `canonical_label`), and the vocabularies `INSURANCE_PRODUCT_LINES`, `COVERAGE_DETERMINATIONS`, `URGENCY_LEVELS`, `DOC_TYPE_SUBCLASSES` | **DROP the module.** Its constants become mailroom profile data. `normalize_corpus_subclass` becomes the generic "catalog fold" (below) |
| `schemas/equivalences.py` | 76 | `normalize_subtype` (CUAD label/alias/8-char-prefix fold), `normalize_doc_subclass` (allowed-set fold) | **PORT** the two fold functions as generic algorithms over profile tables. The equivalence predicates belong to a later chunk (docclass tasks) |
| `schemas/cuad_schema.py` | 1150 | needs only `CONTRACT_PARSE_COMPAT_FIELDS` (two names) | **DROP.** The two names become profile data (`contract.accepted_unscored_fields`) |
| `schemas/doc_classes.py` | 335 | `DOC_CLASSES` registry; reached via `config.py` (`RETIRED_DOC_CLASS_KEYS`) | **DROP.** Its content becomes the mailroom client profile (classes, live or retired, fields with types) |
| `config.py` | 626 | `Settings` (lru-cached, env `LLM_DOJO_SCORING_CONFIG`), `FieldScoringSettings`, `TraceKnobSettings`, `RETIRED_DOC_CLASS_KEYS`, CUAD `CONTRACT_SUBTYPES` and aliases, `SUBTYPE_UNKNOWN` | **DROP.** Replaced by an explicit `ScoringProfile` passed in. No globals, no env var, no cache |
| `pipeline/mailroom.py` | 318 | only `EXTRACT_CLASS_ALIASES` (an empty dict), lazily in `get_field_types` | **DROP.** A class alias becomes optional profile data |
| `pipeline/intake.py` | 417 | listed by AST only because `mailroom.py` imports it | **DROP** (not used by the in-scope code) |

Third-party dependencies:

| Dependency | Where | Old status | Proposal |
| --- | --- | --- | --- |
| `python-dateutil` | date parsing (lazy, required for any date) | core | **core**: an ImportError would propagate anyway |
| `jellyfish` | `_jaro_winkler` (lazy) | core | **core**. Without it the scorer *silently* falls back to difflib and name scores change. Add a test that fails if it is missing |
| `scipy` + `numpy` | Hungarian matching (lazy) | core | **core**. On *any* exception the old code falls back to a greedy matcher, which can give a different `matched` count (see section 5) |
| `structlog` | two `logger.warning` calls | core | **drop**; use stdlib `logging` |
| `PyYAML` | `config.py` | core | **core** (profile loader) |
| `sentence-transformers` | local embedding (lazy) | extra `embeddings` | **optional plug-in**, off by default |
| `openai` | remote OpenRouter embedding fallback (lazy, reads `OPENROUTER_API_KEY`) | extra | **DROP**: a network call inside a scorer. The design says the Dojo never runs a model |
| `pandas`, `matplotlib`, `openpyxl`, `packaging` | not used by this closure | core | not needed for this chunk |

---

## 2. Every hard-coded class, field, map, lookup or global setting, and where it goes

Line numbers are in `llm_dojo_scoring/scoring/field_scoring.py` unless another file is
named.

### 2a. Process-global settings (read through `get_settings()`, which is lru-cached)

| Site | Setting | Proposal |
| --- | --- | --- |
| :172-174 `get_ambiguous_band` | `ambiguous_band` (0.5, 0.85) | `profile.thresholds.ambiguous_band` |
| :177-179 | `bipartite_match_threshold` 0.6 | `profile.thresholds.list_match_threshold` |
| :182-200 | `embedding_model`, `embedding_rescue_below` 0.7, `presence_embedding_threshold` 0.7, `embedding_enabled` False | `profile.embedding.{enabled, rescue_below, presence_threshold}`. The model name belongs to the plug-in's own config, not to the Dojo |
| :203-237 `get_type_bands`, `field_is_ambiguous` | `type_bands` (empty by default; matches the exact type, then the `namespace:` prefix) | `profile.thresholds.type_bands` (`always`, `never` or `[low, high]`) |
| :271-276 | `partial_gt_fields` = {parties, keywords, cuad_clauses, maud_clauses, claim_checklist, action_items} (`config.py:293`) | **per field** in the class schema: `coverage_scoring: recall` (the "partial ground truth" rule). This set is global today, so a field name scores differently in every class; per-class is the cleaner model. For parity the mailroom profile marks those names in every class that has them |
| :279-281 | `containment_fields` = {governing_law, term_length, renewal_terms, subject_matter} (`config.py:303`) | per field: `compare: containment`. Keep the rule that it applies only when the declared type is name, free_text or containment, and that ambiguity uses the declared type |
| :284-293 | `verification_enabled` True, `verification_token_coverage` 0.7 | `profile.audit.{enabled, token_coverage}` |
| :1369 | `trace_knobs.reasoning_routes_presence` True | `profile.presence.route_by_reasoning` |
| :1719 `get_field_types` | `settings.doc_class_field_types` (wired by `configure_from_taxonomy`) plus `pipeline.mailroom.EXTRACT_CLASS_ALIASES` | `profile.field_types(class_key)`; optional `profile.class_aliases` |
| :1947 `capture_trace_knobs` (in `trace_knobs.py:205`, `get_trace_knobs()`) | `capture_confidence`, `capture_reasoning`, `confidence_min`, `confidence_band`, `compute_calibration_error`, `missing_confidence` | `TraceCaptureOptions` dataclass (defaults equal the old defaults), held on the profile or passed in |
| :1449-1585 `_EmbeddingMatcher` | singleton with a class lock; per-process unbounded vector cache; env `OPENROUTER_API_KEY` and `OPENROUTER_BASE_URL` | `SimilarityBackend` Protocol (`similarity(a, b) -> float \| None`) injected by the caller. Default `None` means string-only scoring. No singleton, no env, no network |
| `classification.py:18,35-38` | `RETIRED_DOC_CLASS_KEYS` (court_opinion, due_diligence, compliance_filing) | `class.status: retired` in the profile; retired classes leave the sorter label set |

### 2b. Hard-coded classes and fields

| Site | What | Content or engine | Proposal |
| --- | --- | --- | --- |
| :57-59 `NEVER_SCORED_FIELDS` | `confidence`, `reasoning`, plus `gt_metadata.ANNOTATION_KEYS` (17 Hub metadata keys, `gt_metadata.py:78-104`) | `confidence` and `reasoning` are engine (output contract); `ANNOTATION_KEYS` is content (dataset metadata) | `profile.never_scored_fields` (dataset annotation keys). Engine constant `TRACE_FIELDS = {confidence, reasoning}` |
| :63-71 `RETIRED_PROMPT_KEYS` | key_obligations, termination_clauses, key_provisions, key_points, referenced_communications | content | `profile.retired_fields` (skipped unless the caller's field_types names them) |
| :75-88 `CONTRACT_PARSE_COMPAT_KEYS`, `is_parse_compat_key` | `merger_consideration`, `maud_clauses` on class `contract` (hard-coded string compare at :86) | content (CUAD v1 compatibility) | per class: `accepted_unscored_fields: [...]`. Generic rule: skip unless the explicit field_types names the field |
| :102 `CMS_CLAIM_TYPES` | pde, inpatient, outpatient, carrier | content | the CMS variant's marker vocabulary (below) |
| :110-116 `CMS_ROW_FIELD_TYPES` | claim_type → label, cms_source_table → label, supporting_documents → `entity_list:cms_doc_ref` | content | variant `field_type_overrides` |
| :119-132 `canonical_claim_type` | legacy SAND-042 fold, unused by the scorer | content | **DROP** (kept in the old Dojo only for importers) |
| :135-153 `expected_cms_source_table` | `doc_class == "insurance_claim"`, `CMS_SOURCE_TABLES`, guard `claim_type` is empty or `health` | content | generic **row variant** (see 2c) |
| :156-169 `is_unstated_cms_marker` | `cms_source_table` not stated (also not as a token on `claim_type`): not scored | content rule, generic mechanism | variant `marker.unstated_policy: skip` and `marker.fallback_fields: [claim_type]` |
| :1013-1024 `_ROLE_WORDS` | 23 CUAD party-role words used in partial-GT list matching | content (CUAD labelling habit) | `profile.list_matching.role_words`. Empty means the rule is off |
| :302-307 `_CORPORATE_SUFFIXES`, :333-338 `_CONTAINMENT_STOPWORDS`, :389 `_MONTH_NAMES`, :546-553 currency words, :1247 `_DATE_CANDIDATE_RE`, :861-871 relative-date words | English-language normalization | engine | keep as engine constants in `normalization/`. Optional profile override of suffixes and stopwords; with no override the result is identical |
| :422 pivot 69, :686 cap 0.67, :834-858 date credits (1.0 / 0.67 / 0.33, 45-day window), :591 one-cent money tolerance, :1077 contained-label 3-6 tokens, `disaggregate_clause_spans(min_span_tokens=4)` | scoring rules | engine | engine constants. Changing one is a score change and goes in the score changelog, not in a profile |
| :1428 default presence field `cuad_clauses` | content default | | the presence expectation always carries its `field`; no default in the engine (the labels side supplies it) |
| :1587-1644 `_cms_doc_signature`, `score_cms_doc_ref` (NPI, NDC 5-4-2 padding, facility id, descriptor) | identifier parser for one dataset | **plug-in** (code, not config) | an element scorer registered as `cms_doc_ref` in `mailroom_dojo.field_scoring.plugins.cms` (or a profile `scorer_plugins:` list). The profile names it as `entity_list:cms_doc_ref` |
| :1651-1660 `FIELD_SCORERS` | type → scorer dispatch, with fallback `score_name_field` for an unknown type | engine | `FieldScorerRegistry` (instance, not module global). The mailroom profile registers `cms_doc_ref`. **An unknown type should be a load-time error**, not a silent name-match fallback; for parity, keep the fallback only behind a flag and check that no profile field uses an unregistered type |
| :1682-1694 `_heuristic_field_type` | guesses a type from the field name (date / money words / id words / name) when a GT key is not in field_types | engine, but hidden policy | `profile.unmapped_fields: heuristic \| skip \| error`. The mailroom profile uses `heuristic` for parity (the arbiter and audit_agent goldens and many old tests rely on it). A new client profile should use `skip` or `error` |
| :1822-1890 inside `score_extraction` | `insurance_claim`, `claim_type`, `cms_source_table`, `CMS_DOCUMENTED_ABSENT_FIELDS` | content | row variant (2c) plus a field rule: on `insurance_claim.claim_type`, a predicted value in the CMS token set is scored as `label` even on a non-CMS row (:1864-1869) → `field.vocabulary_type_override: {tokens: [...], type: label}` |
| :1876-1883, 1934-1942 | `canonical_label(doc_class, key, value)` for `label` fields | content (vocabularies) plus engine (folding) | `LabelVocabulary` per label field in the profile (2d) |
| `classification.py:24-31` `_DEFAULT_CLASSES_RE` | 6 classes, ordered most-specific first, pattern `\bword[_ ]?word\b` | content | derived from the profile class list. **Order matters**: the first matching pattern wins, so "correspondence about an insurance claim" gives `correspondence`. The profile list order is the match order; the mailroom profile keeps the old order (merger_agreement, corporate_record, correspondence, compliance_filing[retired], insurance_claim, contract) and then the classes the old table lacks. Optional per-class `synonyms:` patterns for AmFam |
| `classification.py:51` JSON `doc_type` key | sorter output contract | engine | constant, or `profile.classification.label_json_key` (default `doc_type`) |
| `classification.py:20` `ERROR_PREFIX` | failed-row sentinel `"ERROR: "` | engine | engine constant |

### 2c. The CMS special case as a generic "row variant"

What the old code does for an `insurance_claim` row whose expected fields carry a valid
`cms_source_table` (in {carrier, inpatient, outpatient, pde}, with `claim_type` empty or
`health`):

1. `adjuster` and `denial_reasons` are documented absent. An empty expectation is still
   scored (1.0 if the prediction is empty, 0.0 if it states something; nothing if there
   is no prediction at all). A key missing from GT is injected as `""` only when the
   prediction states it (:1830-1835).
2. Field types are overridden: claim_type and cms_source_table become `label`,
   supporting_documents becomes `entity_list:cms_doc_ref`.
3. The marker `cms_source_table` is skipped when the prediction states no table, either
   in that field or as a token on `claim_type`.
4. If the marker field is blank, the token is read from `claim_type` for the marker field
   only (:1884-1890).

Proposed profile shape (content in the mailroom profile, mechanism in the engine):

```yaml
classes:
  - key: insurance_claim
    variants:
      - name: cms_desynpuf
        marker: {field: cms_source_table, values: [carrier, inpatient, outpatient, pde],
                 fallback_fields: [claim_type], skip_when_unstated: true}
        guard:  {field: claim_type, allowed: [health], allow_empty: true}
        documented_absent_fields: [adjuster, denial_reasons]
        field_type_overrides: {claim_type: label, cms_source_table: label,
                               supporting_documents: "entity_list:cms_doc_ref"}
    fields:
      claim_type: {type: name, vocabulary_type_override: {tokens: [pde, inpatient, outpatient, carrier], type: label}}
```

`score_extraction(..., cms_aware=False)` becomes `apply_variants=False` (the auditor
compares two model outputs, so expected is not ground truth).

Where the marker comes from: `gt_metadata.scoring_gt_fields` derives `cms_source_table`
from `expected_subclass` (via `corpus.cms_source_table`). That is label shaping; it goes
behind the labels interface (2e) and is driven by the same variant config
(`marker.derive_from: subclass`).

### 2d. CUAD, MAUD and the label folds: content or engine

| Item | Verdict |
| --- | --- |
| CUAD 25-family keys, labels and aliases (`config.py:32-110`) | content: mailroom profile `cuad_family` vocabulary with `fold: catalog`, a catalog table (key, label) and aliases |
| `normalize_subtype` algorithm (exact key, then alias, then label equal or matching the first 8 characters of the label) | engine: generic `catalog` fold |
| `normalize_doc_subclass` (allowed set, compact match, else `other`) | engine: generic `allowed_set` fold |
| MAUD consideration types; correspondence communication types; corporate record types | content: vocabularies with `fold: allowed_set` |
| `INTENT_LABELS`, `INTENT_ALIASES`, `INTENT_DESCRIPTIONS` | content (descriptions belong to prompts, i.e. the engine) |
| intents `_normalize` (exact compact, alias exact, long alias (8 or more characters) as prefix or substring, canonical prefix, canonical substring (4 or more characters, or 3 or more with a leading digit), `other`) | engine: generic `inventory` fold |
| merger `amend*` → `amend_merger` (`intents.py:216`) | content: `prefix_aliases: {amend: amend_merger}`, applied first |
| coverage_determination outcome aliases (`scored_enums.py:225-232`) | content: `aliases:` with `fold: aliases` |
| `canonical_label` rules: `intent` uses the inventory fold; catalog fields return `""` when the result is `other` or outside the vocabulary; any other enum uses exact compact; an unfoldable value is kept as written | engine: `LabelVocabulary.fold(value) -> str` with `fold ∈ {exact, aliases, inventory, catalog, allowed_set}`. Equality has to be proven with the differential fixture (section 3) |
| CUAD presence (`score_category_presence`, reasoning-trace routing, `disaggregate_clause_spans`) | engine (generic "category presence" over any category→field map). Building expectations from Hub `cuad_clause_labels` belongs to the labels side |
| MAUD question scoring (`content_scoring.score_maud_extraction`, `maud_schema`) | out of scope (later chunk) |
| `merger_agreement` `maud_clauses` partial GT | content (the per-field `coverage_scoring: recall`) |

### 2e. The labels-package interface the Dojo needs

These functions are about turning Hub rows into expected fields, which is labels work:
`parse_gt_fields`, `gt_presence_map`, `scoring_gt_fields` (absent statuses
`not_applicable`, `schema_documented_absence` and `pending_annotation`, the drop-unmapped
rule, CMS marker derivation), `presence_expectations_from_cuad_labels`,
`derive_presence_from_gt`, `normalize_field_values`.

`mailroom_labels` does not exist yet. Define a structural Protocol in the Dojo. The labels
package implements it without importing the Dojo, so the dependency stays one-way and
there is no cycle:

```python
class ExpectedDocument(Protocol):        # one labelled document
    document_id: str; class_key: str; subclass: str | None
    fields: Mapping[str, Any]            # already shaped: absent → "", pending dropped
    field_status: Mapping[str, str]      # hub | interim | near_gold | gold
    presence: Mapping[str, PresenceExpectation] | None
    snapshot_id: str
class LabelSource(Protocol):
    def expected(self, document_id: str) -> ExpectedDocument | None: ...
```

For this chunk, port `scoring_gt_fields` and the presence builders into
`mailroom_dojo/labels_interface/hub_rows.py` as a reference adapter plus test fixture
(clearly marked to move to `mailroom_labels`), so the parity tests can shape GT the same
way.

### 2f. `ScoringProfile` proposal

One immutable dataclass, built from the `scoring:` section of
`config/clients/<client>/profile.yaml` and validated at load time. Unknown keys, an
unregistered field type, a label field without a vocabulary, a variant field that is not
in the class, or a duplicate class all raise `ProfileError` naming the path. It has no
module state and is hashable (a content hash, for the run record).

```text
ScoringProfile
  client: str; version: str; content_hash: str
  classes: tuple[ClassSpec, ...]            # order = classification match order
  class_aliases: Mapping[str, str]
  never_scored_fields: frozenset[str]       # dataset annotation keys
  retired_fields: frozenset[str]
  unmapped_fields: "heuristic" | "skip" | "error"
  thresholds: ambiguous_band, type_bands, list_match_threshold
  audit: enabled, token_coverage
  presence: route_by_reasoning, embedding_threshold
  embedding: enabled, rescue_below           # the backend is injected, not configured here
  list_matching: role_words
  trace_capture: TraceCaptureOptions
  normalization: corporate_suffixes?, containment_stopwords?   # optional; defaults are the engine's
ClassSpec
  key, status (live | retired), sorter_label, synonyms (regex list), subclasses
  fields: tuple[FieldSpec, ...]            # ordered
  accepted_unscored_fields, variants: tuple[RowVariant, ...]
FieldSpec
  name, type ("name", "date", "entity_list:free_text", ...), compare (default | containment),
  coverage_scoring (f1 | recall), vocabulary: LabelVocabulary | None, vocabulary_type_override
LabelVocabulary
  tokens, fold (exact | aliases | inventory | catalog | allowed_set), aliases, prefix_aliases, catalog
```

A new AmFam type is one `ClassSpec` entry with typed fields and thresholds: no code edit.
Only a new *element scorer* (a VIN or policy-number parser) needs a plug-in.

---

## 3. Golden cases

**Finding: no golden case calls the in-scope layers directly.** All 22 task cases go
through `scoring/tasks.py:score_task` and all 21 suite cases through `scoring/suites.py`
(both excluded). Those layers add bootstrap CIs, scorecard honesty, schema coverage,
extraction metrics and so on. So `scores.json` cannot be compared one-to-one in this
chunk.

What reaches the in-scope code, and through which wrapper (from `trace_goldens.py` and
`capture_boundary.py`):

| Golden case | In-scope entry calls | Through (excluded) |
| --- | --- | --- |
| suite `contracts_specialist`, `corporate_records_specialist`, `correspondence_specialist`, `insurance_claims_specialist`, `merger_agreement_specialist` | `score_extraction` ×2 each (cms_aware=True, doc_text=None), `fbeta`, `is_parse_compat_key`, `is_unstated_cms_marker`, `expected_cms_source_table`; insurance also `parse_money`, `score_money_field` | suites, extraction_metrics, scorecard_honesty, extraction_schemas, claims_consistency, maud_schema |
| suite `contract_auditor`, `corporate_records_auditor`, `correspondence_auditor`, `insurance_claims_auditor`, `arbiter`, `audit_agent` | `score_extraction` ×1 (cms_aware=False; arbiter and audit_agent use doc_class `audit` with `finding`/`severity` or `risk`/`summary` free_text) | `suites._score_audit` |
| task `doc_class`, `subtype`, `multiclass`, `court_opinion`, `maud_question`; suite `judge` | `normalize_label`, `per_class_stats`, `macro_accuracy`, `confusion_matrix`, `top_confusions`, `fbeta` | `tasks._label_score`, `_per_class`, `normalize_task_answer` |
| task `docclass`, `maud_docclass`; suite `sorter`, `sorter_reviewer` | `normalize_label`, `fbeta` | tasks, plus `corpus.normalize_corpus_subclass` and `subclass_equivalent` |
| task `pipeline`, `document-pipeline` | `macro_prf`, `per_class_stats`, `fbeta` | `pipeline/mailroom.score_aligned_classification` |
| task `legalbench` | `binary_metrics`, `confusion_matrix`, `top_confusions`, `fbeta` | tasks |
| task `enron_topic`, `content_topic`, `enron_sentiment`, `sentiment` | `confusion_matrix`, `top_confusions`, `normalize_label` (internal) | `content_scoring._label_content_score` |
| task `intake`; suite `intake`, `pdf_transcriber`, `image_extractor` | `score_field` (free_text), `exact_match`, `accuracy` | `pipeline/intake`, `asr`, `suites._score_transcription` |
| **No in-scope calls:** task `cer`, `wer`, `transcription`, `chained`, `contracteval`, `maud_extraction`, `maud_clause`; suite `archivist`, `boss`, `reporter` (emit-only), `local_vs_api` | none | entirely excluded layers |

Boundary capture totals: 704 calls into in-scope modules (25 distinct functions),
including 16 `score_extraction`, 59 `normalize_label`, 100 `fbeta`, 11 `confusion_matrix`,
6 `macro_accuracy`, 2 `macro_prf`, 1 `binary_metrics`, 1 `accuracy`, 5 `score_field`, 241
`is_empty_value`, and 10 `scoring_gt_fields`. All of them serialize to JSON except 30
`convertyear` callbacks from dateutil (internal; ignore). Every captured
`score_extraction` result's `field_scores` appears verbatim in `scores.json` (for example
`/suites/contracts_specialist/score/field_scores` and `.../score_document/extraction/field_scores`).
So the boundary capture is tied to the committed baseline.

Field-type provenance: 13 of the 16 calls pass exactly the registry map
(`doc_classes.field_types`). `corporate_records_specialist` passes a **40-field** map
(from `corporate_record_schema`), not the registry's map. `arbiter` and `audit_agent`
pass ad hoc 2-field maps for a non-class `audit`. So the mailroom profile must hold the
40-field corporate_record map if the later suite port is to match; the fixture passes
field_types explicitly, so this chunk does not depend on it.

**Coverage the goldens give the in-scope code (old Dojo, coverage.py with branches):**
field_scoring 46%, classification 74%, gt_metadata 56%, scored_enums 39%, intents 12%.
No golden passes `doc_text`, so the audits (`audit_list_field`, `audit_scalar_field`,
`verify_list_items`, `_date_grounded_in_doc`) are **never exercised**, and neither are
`score_category_presence` or embedding rescue. The old direct tests (477 tests in 11
files, 2.8 s) reach field_scoring 78%, classification 90%, gt_metadata 97%, scored_enums
100%, intents 85%.

### Proposed re-baselined golden check (identical scores)

1. **Boundary goldens (identity with `scores.json`).** Commit
   `packages/dojo/tests/fixtures/golden/old_dojo_boundary_calls.json`: each recorded
   in-scope call with function, args, result and golden case. The header carries the old
   Dojo SHA `23f8234c…`, version 0.30.1, and the sha256 of the old `inputs.json` and
   `scores.json`. `packages/dojo/scripts/capture_old_dojo_boundary.py` produces it (it is
   `capture_boundary.py` here, made reusable). It runs only against an old-Dojo venv and
   never in CI, so nothing is pinned or vendored: the fixture is data.
   `tests/test_golden_parity.py` replays every `score_extraction`, `normalize_label`,
   `per_class_stats`, `confusion_matrix`, `top_confusions`, `macro_accuracy`, `macro_prf`,
   `binary_metrics`, `accuracy`, `exact_match`, `fbeta`, `score_field`, `parse_money` and
   `score_money_field` call through the new API with the mailroom profile, and compares
   canonical JSON (`freeze_goldens.canonicalize` rules: sorted keys, sets rejected,
   non-finite floats rejected, `None` distinct from `0.0`). It also re-asserts that the
   recorded `field_scores` sit at the stated `scores.json` paths, so the fixture cannot
   drift from the old baseline unseen.
2. **Differential corpus (the coverage the goldens lack).** Run the same capture hook
   over the old direct tests (`test_field_scoring.py`, `test_classification.py`,
   `test_gt_metadata.py`, `test_scored_enums.py`, `test_intents.py`,
   `test_scoring_fixes_127.py`, `test_v026_truth_fixes.py`,
   `test_scoring_fixes_133_132.py`, `test_paired_cardinality.py`,
   `test_parse_compat_enum_key.py`, `test_trace_knobs.py`) and over seeded generated
   inputs: date strings (ISO, numeric, prose, ordinal, yearless, ranges, weekdays,
   two-digit years, blanks), money strings, names with suffixes, lists with role words,
   CMS identifiers, and `doc_text` audits. Freeze the (input, old output) pairs as
   `differential_cases.jsonl`. Exclude calls that use monkeypatches or embeddings. The
   new suite replays them all, which brings parity coverage to about 78% or more of the
   old field_scoring lines, including the audits.
3. **Intended differences.** `packages/dojo/docs/score-changelog.md`, plus a
   machine-read `intended_differences.json` keyed by (fixture, case id) with a reason and
   the new value. The parity test fails on any difference not listed, and fails on a
   listed difference that no longer occurs (so stale entries do not hide anything).
   Expected entries: none for the boundary goldens. Possible entries: the structlog to
   logging change (not a score), and dropping the remote embedding (no golden uses
   embeddings, so no score changes).
4. When the later chunk ports suites and tasks, add a check that regenerates the whole
   old `scores.json` from the new package; this chunk's boundary fixture then becomes a
   subset.

---

## 4. Proposed layout and work split

```
packages/dojo/
├── pyproject.toml          # mailroom-dojo; deps: python-dateutil, jellyfish, scipy, numpy, PyYAML
│                           # extras: embeddings = [sentence-transformers]; dev = [pytest, hypothesis]
├── README.md
├── src/mailroom_dojo/
│   ├── __init__.py
│   ├── profile/
│   │   ├── __init__.py               # ScoringProfile, ClassSpec, FieldSpec, LabelVocabulary, RowVariant, ProfileError
│   │   ├── loading.py                # load_scoring_profile(path | mapping) -> ScoringProfile (validation, content hash)
│   │   └── label_folding.py          # LabelVocabulary.fold: exact | aliases | inventory | catalog | allowed_set
│   ├── values/
│   │   ├── __init__.py
│   │   └── emptiness.py              # parse_json_container, is_empty_value (port of gt_metadata helpers)
│   ├── normalization/
│   │   ├── text.py                   # normalize_text, tokenize, containment_tokens, token_f1, token_set_ratio, jaro_winkler
│   │   ├── dates.py                  # DateParts, parse_date_parts, parse_date, fixed pivot, relative and blank detection
│   │   └── money.py                  # parse_money
│   ├── field_scoring/
│   │   ├── __init__.py               # public re-exports
│   │   ├── scalar_scorers.py         # score_id/date/money/name/label/free_text/containment, date_match_similarity
│   │   ├── scorer_registry.py        # FieldScorerRegistry, is_entity_list, element type parsing
│   │   ├── similarity_backend.py     # SimilarityBackend Protocol, with_embedding_rescue, optional SentenceTransformerBackend
│   │   ├── entity_lists.py           # EntityListScore, score_entity_list (Hungarian + explicit greedy option)
│   │   ├── audits.py                 # verify_list_items, audit_list_field, audit_scalar_field, field_audit_support
│   │   ├── category_presence.py      # disaggregate_clause_spans, score_category_presence
│   │   ├── row_variants.py           # RowVariant resolution (generic form of the CMS rules)
│   │   ├── trace_capture.py          # TraceCaptureOptions, capture_trace (port of trace_knobs)
│   │   ├── extraction.py             # ExtractionScoreResult, score_extraction(profile, class_key, predicted, expected, ...)
│   │   └── plugins/cms_identifiers.py  # score_cms_doc_ref (mailroom-dataset plug-in)
│   ├── classification/
│   │   ├── __init__.py
│   │   ├── labels.py                 # ClassLabelSet.from_profile, normalize_label, exact_match, failure, ERROR_PREFIX
│   │   ├── paired.py                 # require_equal_lengths
│   │   └── metrics.py                # accuracy, fbeta, per_class_stats, macro_accuracy, confusion_matrix, binary_metrics, macro_prf, confusion_accuracy, top_confusions, class_distribution
│   └── labels_interface/
│       ├── __init__.py               # LabelSource, ExpectedDocument, PresenceExpectation Protocols
│       └── hub_rows.py               # reference adapter: scoring_gt_fields, presence_expectations_from_cuad_labels (moves to mailroom_labels later)
├── tests/ ...                        # parity, differential, R4 synthetic type, faults, properties
└── scripts/capture_old_dojo_boundary.py
config/clients/mailroom-dataset/profile.yaml   # the mailroom content (classes, 40-field corporate map, vocabularies, CMS variant)
```

Dependency order, with no cycles: `values` and `normalization` → `profile` (label folding
uses `normalization.text`) → `field_scoring` (scalar → registry → entity_lists and audits
→ presence → variants → extraction). `classification` depends only on `profile`.
`labels_interface` depends on `values` and `profile`. Nothing imports
`mailroom_engine`. Only `labels_interface` may later import `mailroom_labels`.

### Task A: scalar scorers, profile, normalization

Owns `values/`, `normalization/`, `profile/` (dataclasses, loader, validation, label
folding), `field_scoring/scalar_scorers.py`, `scorer_registry.py`,
`similarity_backend.py`, `plugins/cms_identifiers.py`, and
`config/clients/mailroom-dataset/profile.yaml` (all the content from section 2).

Exports: `ScoringProfile`, `ClassSpec`, `FieldSpec`, `LabelVocabulary`, `RowVariant`,
`ProfileError`, `load_scoring_profile`, `parse_json_container`, `is_empty_value`,
`normalize_text`, `parse_date`, `parse_money`, `score_id_field`, `score_date_field`,
`score_money_field`, `score_name_field`, `score_label_field`, `score_free_text_field`,
`score_containment_field`, `score_cms_doc_ref`, `FieldScorerRegistry`, `is_entity_list`,
`SimilarityBackend`, `with_embedding_rescue`.

Parity: the differential cases for scalar scorers, `parse_*` and `canonical_label` (fold).

### Task B: entity lists, audits, presence, `score_extraction` (needs A)

Owns `entity_lists.py`, `audits.py`, `category_presence.py`, `row_variants.py`,
`trace_capture.py`, `extraction.py`.

Exports: `EntityListScore`, `score_entity_list`, `verify_list_items`, `audit_list_field`,
`audit_scalar_field`, `field_audit_support`, `disaggregate_clause_spans`,
`score_category_presence`, `TraceCaptureOptions`, `capture_trace`,
`ExtractionScoreResult`, `score_field`, `field_is_ambiguous`, and
`score_extraction(profile, class_key, predicted, expected, *, field_types=None, doc_text=None, audit_predicted=None, apply_variants=True, similarity=None)`.

`field_types=None` means "use the profile". An explicit map keeps the old opt-in
semantics for retired and parse-compat keys.

Parity: the 16 boundary `score_extraction` calls (identical `to_dict()`, including
`trace`), plus differential cases for lists, audits with `doc_text`, and presence.

### Task C: classification, label-source Protocol, R4 end-to-end, golden harness (classification part is independent of A and B; the R4 test and harness run last)

Owns `classification/`, `labels_interface/`, `scripts/capture_old_dojo_boundary.py`,
`tests/fixtures/golden/*`, `tests/test_golden_parity.py`, `tests/test_new_document_type.py`,
`tests/test_boundaries.py` (no engine import; only record and labels allowed),
`docs/score-changelog.md`.

Exports: `ClassLabelSet` (from profile: ordered patterns, retired classes excluded),
`normalize_label(value, labels)`, `exact_match(output, expected, labels)`, `failure`,
`accuracy`, `fbeta`, `per_class_stats`, `macro_accuracy`, `confusion_matrix`,
`binary_metrics`, `macro_prf`, `confusion_accuracy`, `top_confusions`,
`class_distribution`, `require_equal_lengths`, `ERROR_PREFIX`, `LabelSource`,
`ExpectedDocument`, `PresenceExpectation`.

R4 test: write a YAML profile in tmp with a synthetic class (`auto_policy_declaration`:
a sorter label, fields `policy_number: id`, `effective_date: date`,
`premium: money`, `insureds: entity_list:name`, `coverage_tier: label` with a
vocabulary, thresholds). Load it, classify noisy sorter outputs, score extractions, and
assert exact numbers. Assert no file under `src/` changed and that the class name appears
nowhere in `src/`. Also test that removing the class or adding a typo type fails at load
with `ProfileError`.

### Circularity flags

- **Shared profile schema.** The client profile is read by labels, the Dojo and the
  engine, but the engine may import only `record`, and `record` must stay domain-free. So
  no one Python profile model can serve all three without breaking the rule. Proposal:
  each package parses only its own section (`scoring:` for the Dojo); a JSON Schema in
  `config/` validates the whole file; a workspace test checks the sections agree (class
  keys, field names). **Decision needed by the orchestrator.**
- `classification` and `field_scoring` do not import each other. `canonical_label`
  needs only `profile`. The old `gt_metadata → corpus → config → doc_classes` chain and
  the `field_scoring → pipeline.mailroom → pipeline.intake → field_scoring` cycle (made
  lazy in the old code) both disappear.

---

## 5. Risks

| Risk | Detail | Mitigation |
| --- | --- | --- |
| Global cached settings | `config.load_settings` is `lru_cache`d. `configure()` and `configure_from_taxonomy()` **mutate the cached object in place**, and `LLM_DOJO_SCORING_CONFIG` overrides everything. The old test isolation depends on `clear_settings_cache()` | Explicit immutable profile argument. A test that two profiles scored in one process (and threads) give independent results |
| Module globals | `_DATE_PARSERINFO` lazy global; `_EmbeddingMatcher._instance` singleton with an unbounded vector cache and a class lock; `warm_embedding_model` starts a daemon thread | parserinfo as a module constant built at import; the similarity backend owned by the caller with a bounded cache; no threads in the Dojo |
| Network | remote OpenRouter embeddings when `OPENROUTER_API_KEY` is set and sentence-transformers fails to load. Default off, but the key is enough once enabled | drop it; a test asserting that no socket is opened during scoring (monkeypatch `socket.socket` to raise) |
| Hidden model download | `SentenceTransformer(name)` downloads from the HF Hub on first use | plug-in only, never in the default install or the tests |
| Silent degradation | jellyfish missing → difflib; scipy failure → greedy matching, and the `except Exception` also swallows *bugs* in `_element_similarity`; embedding errors → string score; `_jaro_winkler` swallows every exception | hard deps; matcher selection explicit; catch only ImportError; log and count fallbacks on the result; tests that inject a failing similarity and assert the exact reported fallback (R8) |
| Clock dependence | the old code was fixed to never read the clock (fixed probe dates, fixed pivot 69). The old tests pin this with monkeypatched `dateutil.parser.datetime` and `time.localtime` (`test_field_scoring.py:130,704`) | port those tests as-is (they patch the dateutil module, not Dojo globals); add a property test that parsing gives the same result under different fake clocks |
| Determinism | `linear_sum_assignment` is deterministic, but ties between equal similarities can depend on input order; set iteration feeds `sorted()` where it matters; `score_category_presence` sorts categories | property test: the list score is unchanged under a shuffle of predicted items (it should be for `matched`; verify with the old Dojo first, since it may not hold for ties) |
| Unknown-type fallback | an unknown field type silently scores as `name`; GT keys not in field_types are typed by name heuristics | profile validation error; `unmapped_fields` policy explicit; the mailroom profile keeps `heuristic` for parity |
| Normalization order | `normalize_label` is first-match-wins over an ordered regex table; the order is content | profile list order is the match order; parity on all 59 captured calls plus the old `test_classification.py` |
| Global `partial_gt_fields` | a field name is partial-GT in every class | per-field in the new model; the mailroom profile must mark it on every class that has these names, or parity breaks for, e.g., `parties` on corporate_record |
| corporate_record field map | the specialist suite uses a 40-field map, the registry a different one | choose one for the profile and record the choice; it matters to the suite port, not to this chunk's fixture |
| Hard-to-port tests | `test_trace_knobs.py` uses `configure(...)` 11 times (becomes options arguments); `test_consumer_compat.py` pins old public names and `_EmbeddingMatcher` internals (drop: no consumer contract here); `test_golden_scores.py` is built around suites and tasks (replaced by the parity tests); `test_doc_classes.py` swaps registry entries (becomes profile-loading tests); about 15 other files reach the in-scope code only through `get_suite` or `score_task` (later chunk) | port the 11 direct files listed in section 3 as behavior tests; capture their calls into the differential fixture |
| Labels not built yet | `mailroom_labels` does not exist; the README allows dojo → labels, but nothing can be imported yet | the Protocol lives in the Dojo; the reference Hub adapter is marked to move |
| Float formatting | many results use `round(x, 4)`, so parity has to compare exact floats, not approximately | canonical JSON equality, as in `freeze_goldens` |

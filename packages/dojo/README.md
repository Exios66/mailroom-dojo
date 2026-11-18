# mailroom-dojo

The `mailroom_dojo` package: the pure scoring layers of the Mailroom Dojo, driven by a
per-client **scoring profile**. It decides what "correct" means for one extracted field,
one label or one document class. It holds no global state, reads no environment
variable or clock, and opens no network connection. It imports `mailroom_record` (and,
once it lands, `mailroom_labels`) and nothing from any engine or from `llm_dojo_scoring`.

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

## Tests

See `docs/test-strategy-dojo-core.md`. The differential corpus
(`tests/fixtures/differential/scalar_cases.jsonl`) holds 21,009 inputs with the old Dojo's
outputs; `scripts/capture_old_dojo_scalar_cases.py` regenerates it from an old checkout
and never runs in CI.

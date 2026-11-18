# Golden parity with the old Dojo

How `mailroom_dojo` proves it scores exactly like llm-dojo-scoring 0.30.1 (commit
`23f8234c`), how the fixtures are made, and the old classification behaviours kept on
purpose. Scalar-layer parity (task A) is described in `score-changelog.md` and
`docs/test-strategy-dojo-core.md`.

## Fixtures

All under `packages/dojo/tests/fixtures/golden/`, all produced by
`packages/dojo/scripts/capture_old_dojo_boundary.py` from an old checkout. Each header
records the old commit, the old version and the sha256 of the old
`tests/fixtures/golden/inputs.json` (`59292c35…`) and `scores.json` (`42c7f3c6…`).

| File | What it holds | Replayed by |
|:---|:---|:---|
| `old_dojo_boundary_calls.json` | The 674 calls that cross into the old in-scope modules (field scoring, classification, paired lengths, GT metadata, scored enums, intents) from an out-of-scope caller while the old Dojo regenerates its golden baseline: 24 functions, including the 16 `score_extraction` calls, with arguments copied at call time and results. Each `score_extraction` call names the `scores.json` paths where its `field_scores` appear and their hash. 30 dateutil `convertyear` callbacks are left out (not data; counted in the header). | `test_golden_parity.py` |
| `classification_cases.jsonl` | 7,774 classification cases: every call the old `test_classification.py` and `test_paired_cardinality.py` made, plus seeded sorter outputs (JSON objects with `doc_type`, fenced and quoted text, mixed case, near misses, `ERROR:` rows, None, numbers, unicode), paired lists, raw binary flags, F-beta edge values, ragged matrices and length mismatches. | `test_classification_parity.py` |
| `hub_row_cases.jsonl` | 1,194 Hub-row shaping cases: every call the old `test_gt_metadata.py` made to the GT shaping helpers, plus seeded Hub rows (presence maps in every form, stringified containers, annotation keys, CMS subclasses) and clause-label maps. | `test_labels_interface.py` |
| `intended_differences.json` | The ledger of intended differences, keyed by fixture and case. Empty: nothing differs. | all three |

The capture refuses to write unless the old golden run, regenerated under the capture
hook, equals the committed `scores.json` byte for byte, and unless every
`score_extraction` result is found in `scores.json`.

## Comparison rules

- Results are compared as canonical JSON (sorted keys, tagged non-finite floats, tuples
  and sets), so `1` is not `1.0` and `None` is not `0.0`. Results that are objects are
  compared through their `to_dict()`.
- An exception matches by type; for the paired-length check (`expected length …`) and
  for the Hub-row helpers the message must match too.
- A difference fails unless it is in `intended_differences.json` with its reason and the
  new outcome. A listed difference that no longer occurs, or now gives another value,
  also fails.
- `test_golden_parity.py` re-hashes every recorded `field_scores` against the hash taken
  from `scores.json`. With `OLD_DOJO_CHECKOUT=<old checkout>` it also reads the real
  `scores.json`, checks its sha256 and looks each `field_scores` up at its recorded paths.

Old functions map to the new API as follows: classification functions take a
`ClassLabelSet` built from the mailroom profile; `score_field(type, pred, exp,
partial_gt=…)` is `score_field(profile, type, pred, exp, recall_coverage=…)`;
`score_extraction(doc_class, …, cms_aware=…)` is `score_extraction(profile, class_key, …,
apply_variants=…)`; `is_parse_compat_key` is `is_accepted_unscored_field`;
`expected_cms_source_table` and `is_unstated_cms_marker` are `active_row_variant` and
`is_unstated_marker` over the profile's CMS variant; the GT helpers are the Hub-row
adapter (`parse_gt_fields` → `parse_hub_fields`, `gt_presence_map` →
`hub_presence_statuses`, `normalize_field_values` → `parse_field_values`,
`scoring_gt_fields` → `scoring_fields_from_hub_row`,
`presence_expectations_from_cuad_labels` → `presence_expectations_from_clause_labels`,
`derive_presence_from_gt` → `presence_expectations_from_hub_row`).

## Regenerating

Only when the old Dojo or this generator changes, never in CI, from an environment that
can import the old Dojo (see the port inventory for how that venv is built):

```bash
PYTHONDONTWRITEBYTECODE=1 <old-venv>/bin/python packages/dojo/scripts/capture_old_dojo_boundary.py \
    --old-dojo <llm-dojo-scoring checkout at 23f8234c> \
    --output-directory packages/dojo/tests/fixtures/golden
```

It prints the old commit, the call and case counts per function and the
`score_extraction` calls not found in `scores.json` (must be none). The seed is fixed
(`--seed`, default 20261118), so a rerun on the same old commit and Python version writes
the same files. The fixtures were captured with Python 3.12 and are replayed with 3.13;
Unicode case mapping could in principle differ between versions for exotic characters.

## Classification: found, not changed

Old behaviour kept for parity. Each is pinned by a test or by the corpus, so changing one
is a deliberate, listed score change.

- **`per_class_stats` reads a prediction as text first**, so a `None` prediction is the
  label `none`, while `accuracy`, `confusion_matrix`, `binary_metrics` and `macro_prf`
  read it as `""`. The per-class counts and the confusion matrix can disagree on None.
- **`macro_prf` (normalized) normalizes twice**: it normalizes the pairs and then
  `per_class_stats` normalizes them again. `normalize_label` is not idempotent (an output
  `"{"doc_type": "a"}"` in quotes becomes the JSON text, then `a`), so such a class can
  drop out of `per_class` and `n_classes`.
- **A JSON output nested deeper than the recursion limit raises `RecursionError`**:
  only JSON decode errors are caught.
- **JSON keys are matched case-blind** because the whole output is lowercased before it
  is parsed (`{"DOC_TYPE": "CONTRACT"}` is `contract`); a falsy `doc_type` keeps the whole
  JSON text, which is then searched for class names.
- **The error prefix is exact**: `" ERROR: x"` and `"Error: x"` are labels, not failed rows.
  `binary_metrics` does not drop failed rows; the other paired metrics do.
- **`macro_accuracy` is the mean of rounded per-class accuracies** (1.0 and 0.6667 give
  0.8334, not 0.8333).
- **`confusion_matrix` with a repeated explicit label** keeps an empty row and column for
  the first copy; **`top_confusions` with a negative `k`** drops cells from the end.
- **One-shot iterators are refused with `TypeError`** (they have no length), not read.

## Classification design changes (no score change on the fixtures)

- The class table is the profile's: live classes in `classification.match_order`,
  retired classes excluded. A class key's pattern is its words joined by an optional
  space or underscore (`\bmerger[_ ]?agreement\b`), identical to the old table for every
  mailroom class.
- A sorter label that differs from the key is also matched exactly and as words, and
  `synonyms` are regular expressions matched case-blind. The mailroom profile uses
  neither. Two live classes claiming the same exact output, or a profile with no live
  class, is a `ProfileError`.
- There is no default label set: every function that reads labels takes one.

## Order dependence when classes are added

`normalize_label` is first match wins. `test_new_document_type.py` checks what holds:
appending classes after the existing match order changes no output that already read as
an existing class (every corpus output is checked), and outputs that matched no class may
now read as a new class. Exact class keys and JSON `doc_type` values never move. What
does not hold: a class placed **before** an existing one in `match_order` takes text that
names both ("contract for an auto policy declarations page" becomes the new class).
Extraction scores of the existing classes do not depend on other classes at all: the 16
golden extractions are identical with two classes added, appended or first.

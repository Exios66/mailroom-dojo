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
- **Resampling parameters are checked.** `n_boot` must be an integer of at least 1
  (`n_boot=0` raised `IndexError` inside the old resampler), `alpha` must be in `(0, 1)`
  (`alpha=1.5` gave an interval with `lo` above `hi`), and `seed` must be an integer (the
  old Dojo seeded `random.Random` with text such as `"abc"` and returned an interval). As in
  the old Dojo, fewer than two usable values give `None` first, so `bootstrap_ci([0.5],
  n_boot=0)` and `delta_significance([0.5], [0.1, 0.2], alpha=nan)` are still `None`; with
  enough values a bad parameter raises. A NumPy integer `seed` or `n_boot` is read as a
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
  or `OverflowError` from `int()`. The corpus holds no NaN or infinite support, so the
  ledger has no entry for it; `tests/test_statistics_faults.py` pins both behaviours.
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

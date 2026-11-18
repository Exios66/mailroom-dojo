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

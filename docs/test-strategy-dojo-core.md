# Test strategy: `mailroom_dojo` scoring core

What the Dojo core must prove, how each claim is tested and where the test lives. The plan
is `docs/plans/2026-11-18-dojo-scoring-core-plan.md`; the port inventory is
`docs/plans/2026-11-18-dojo-scoring-port-inventory.md`. This file covers task A (profile,
values, normalization, scalar scorers, scorer registry, similarity backend, CMS plug-in);
tasks B and C add their sections.

## Principles

- **A wrong number here is a wrong number everywhere.** Parity with the old Dojo
  (llm-dojo-scoring 0.30.1, commit `23f8234c`) is proven on data, with exact floats, before
  anything is improved. Old behaviour that looks wrong is kept and listed under "Found, not
  changed" in `packages/dojo/docs/score-changelog.md`.
- **Failure is never silent.** A malformed profile raises `ProfileError` at the exact YAML
  path; an unregistered field type raises unless the caller asks for a reported fallback;
  a failing similarity backend keeps the string score and records a `SimilarityFallback`.
  Each failure has a test that asserts the exact exception or report, not just "it failed".
- **Invariants, not only examples.** `hypothesis` drives hostile inputs through every scorer
  (bounded float, no exception), every fold (result inside the vocabulary), the profile
  hash (stable under any key reordering) and the date parser (same answer under different
  fake clocks).
- **No global state, no network, no clock.** Checked statically (AST over the whole source)
  and dynamically (sockets forbidden while the corpus is scored; threads with two profiles).
- **Tests must fail when the code is wrong.** Mutation checks (below) show it.

## Claims and tests

| Claim | Test |
|:---|:---|
| Every scalar layer gives the old output exactly | `test_differential_scalar_parity.py`: 21,009 cases (2,748 calls captured from the old direct tests, the rest seeded generated inputs), 37 functions, compared as canonical JSON |
| The corpus is what it says | same file: header names the old commit and version; counts per function match the lines; both sources present |
| The mailroom profile equals the old registry | `test_profile_loading.py`: classes and order, live/retired, field maps (corporate_record's 40 fields), name rules, CMS variant, defaults |
| Every malformed profile is refused at its path | `test_profile_loading.py`: 80 failure cases (unknown keys at every level, unregistered types, label without vocabulary, duplicate class/variant/token/sorter label, variant fields not in class, NaN/inf/bool/text/out-of-range thresholds, reversed bands, bad aliases and catalogs, duplicate YAML keys, YAML 1.1 booleans as keys, invalid YAML) |
| A profile is immutable and identified by content | `test_profile_loading.py`: frozen attributes and mappings; hash equal across loads, file vs mapping, any key order (hypothesis); changes with any rule, value or ordered list |
| Label folding is closed and ordered | `test_label_folding.py`: each fold method, tie order, fixed points of every mailroom token, results inside the vocabulary for any input (hypothesis) |
| Scorers are bounded and behave as documented | `test_scalar_scorers.py`: documented examples, `[0, 1]` float for any hostile input, self-match, symmetry where it holds, the directional date rules |
| Dates never read the clock | `test_normalization.py`: fixed two-digit pivot under four fake years; property test under two fake clocks; a clock that raises is never touched |
| Hard dependencies fail loudly | `test_normalization.py`: importing without jellyfish, python-dateutil or PyYAML is an import error |
| Similarity backend failures are reported | `test_similarity_backend.py`: raises, NaN, text, bool, list, Decimal, out of range and infinity each keep the string score and record one fallback with exact reason and detail, plus a warning; None declines silently |
| Two profiles do not interfere | `test_similarity_backend.py`: eight threads, two thresholds, 2,400 scores, each thread sees only its own threshold and fallbacks |
| No network | `test_similarity_backend.py`: `socket.socket`, `create_connection` and `getaddrinfo` raise while the profile loads and a seventh of the corpus is scored |
| sentence-transformers is optional and local | `test_similarity_backend.py`: lazy import (subprocess check), `local_files_only=True`, missing extra named in the `ImportError`, bounded cache, clipped cosine |
| The registry resolves every mailroom type | `test_scorer_registry.py`: built-ins equal the loader's type names; plug-ins only when declared; fallback only when asked, and flagged |
| No global state | `test_no_global_state.py`: no environment reads, `global`, cache decorators, clock calls, network/thread/LLM/old-Dojo imports, or mutable module-level values; a positive control proves the checks see each pattern |

## Mutation check (task A)

A scratch script applied 17 single-line mutations to the source, one at a time, and ran the
suite: dropping a null token, ignoring NaN as empty, `<=` to `<` on the money tolerance,
the two-digit-year pivot, the date partial credit, the inventory alias length, the token
ranking order, the backend range check, the rescue threshold comparison, the NaN check,
the unknown-key check, the number range check, the band order check, unsorted hash keys,
an order-sensitive `FrozenMapping` hash, a money name word and the NDC padding. All 17
were caught.

## Running

```bash
.venv/bin/python -m pytest packages/dojo/tests -q -n 4               # whole suite, seconds
HYPOTHESIS_PROFILE=thorough .venv/bin/python -m pytest packages/dojo/tests -q -n 4   # ten times more examples
```

The capture script `packages/dojo/scripts/capture_old_dojo_scalar_cases.py` needs an old
Dojo checkout and its environment; it is a maintenance tool and never runs in CI.

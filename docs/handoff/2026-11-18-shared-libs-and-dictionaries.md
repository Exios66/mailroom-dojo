# Handoff: shared libraries, tooling and dictionaries

Branch: `claude/project-thread-221r59` (stacked on PR #2's branch `claude/project-thread-h3rj4z`; #3 is stacked on it too). Ready PR: #6 (base = #2's branch; retarget to `main` after #2 and #3 merge).

## State

Built and merged on the branch, `make check` green on the head (run with `PYTHONPATH=/home/user/llm-dojo-scoring` so the Dojo cross-check runs: 1024 passed; without it 1023 passed, 1 skipped):

- `packages/common` (`mailroom_common`): errors, strict YAML, config (overlays, hash, `${VAR}`), hashing (parity test with record), JSONL IO, ids, clock, logging with redaction, model-client interface and retry, plug-in registry, test kit and pytest plug-in (entry point `pytest11`). README marks models, retry, registry and logging as provisional.
- `packages/dictionaries` (`mailroom_dictionaries`): 19 versioned vocabularies with a validating loader, version lock, source-value drift fixture, glossary (`docs/glossary.md`, generated).
- Tooling: root `pyproject.toml` (ruff, mypy strict for new packages, codespell), `Makefile` (`make help`), `.pre-commit-config.yaml`, `scripts/check_boundaries.py`, `check_layout.py`, `check_no_session_links.sh`, root `tests/`, CircleCI `lint` job, ADR-0007, README layout map.
- Two independent Opus review rounds on the first build found no blockers and 9 majors (torn-tail append, secret in error chain, redaction gaps, alias mismatch with the Dojo scorer, missing vocabularies, override rules, drift guard); all were fixed by two Opus fixers with regression tests.

## Review log

- Build: four Opus builders (common core, common runtime, dictionaries, tooling), each with mutation checks.
- Round 1: two independent Opus reviewers (common; dictionaries and tooling): no blockers, 9 majors, all fixed by Opus fixers with red-then-green regression tests.
- Bot review (CodeAnt) on PR #6: 5 findings, all verified real and fixed with tests (YAML merge-key override, test-writer symlink escape, CRLF at the JSONL limit, quoted secrets with spaces, mutable request messages); threads resolved without replies.
- CodeAnt nitpicks (8): two fixed (self-referential values in log redaction and in config), the rest left as known gaps: frozen config models still hold mutable dict/list fields, `config_hash` of a non-dict Mapping, torn-tail flag on an oversized last line, run-id test uses a 32-bit suffix over 50 draws (collision odds about 3 in 10 million), glossary headings that differ only in punctuation share an anchor, `check_layout` treats an optional path that is a file as fine.
- Round 2: one independent Opus reviewer on the fix commits: 1 blocker (layout map missing `docs/handoff/`), 2 majors (quadratic redaction regexes, text-redaction leaks), several minors. All fixed; redaction now linear (1 MB adversarial inputs under 2 s, tested) and run-together keys like `dbpassword` are masked.

## Not done

- PR #6 is open and ready (body verified clean of footers and links). Handle bot review comments by verifying them, fixing real ones with tests, and resolving threads without replying (no footers).
- CircleCI must be set up for the repo (JJB's to-do), then green on the final head; all review threads resolved; clean session-link sweep (`bash scripts/check_no_session_links.sh`); squash with a clean message, after #2 and #3.
- Known gaps left on purpose: alias-expansion cap in config; `iter_jsonl` scalar-tail edge case (a truncated `12` parses as a row); the `label_statuses` override guard lives in `DictionarySet.with_overrides` only (a hand-built `DictionarySet` or `load_dictionary_dir` can still carry a re-ranked copy, but the manifest shows `origin: override`); `append_jsonl` refuses a whitespace-only tail and there is no repair helper; `append_jsonl` is 1.3 to 1.6 times slower than before because of its `flock`; NFKC folding diverges from the Dojo for non-ASCII spellings (documented in the dictionary module).

## Follow-ups for other threads (not done here)

- `packages/record`: import `mailroom_common.hashing` and adopt `jsonl`; add `py.typed`; fix 5 mypy strict errors and 9 unformatted files (ruff currently has per-file ignores for record); a real Python 3.11 bug at `packages/record/tests/test_record_machine.py:197` (backslash in an f-string); delete its duplicate `test_boundaries.py`; change record's row in `scripts/check_boundaries.py` to allow `common`.
- Labels and Dojo threads: read status words, field types and closed vocabularies from `mailroom_dictionaries` (both were told the import path); profile references vocabularies by name via `doc_classes.closed_label_fields`.
- Engine: map provider SDK errors onto `ProviderError` subclasses; Modal cold-start backoff is adapter-level.
- The human review desk (JJB, 2026-11-18 13:23 UTC) is a separate project: its own node plus a desk package, writing human corrections to the record. Not started.

# Handoff: shared libraries, tooling and dictionaries

Branch: `claude/project-thread-221r59` (stacked on PR #2's branch `claude/project-thread-h3rj4z`; #3 is stacked on it too). No PR is open for this branch yet.

## State

Built and merged on the branch, `make check` green on the head (run with `PYTHONPATH=/home/user/llm-dojo-scoring` so the Dojo cross-check runs: 1024 passed; without it 1023 passed, 1 skipped):

- `packages/common` (`mailroom_common`): errors, strict YAML, config (overlays, hash, `${VAR}`), hashing (parity test with record), JSONL IO, ids, clock, logging with redaction, model-client interface and retry, plug-in registry, test kit and pytest plug-in (entry point `pytest11`). README marks models, retry, registry and logging as provisional.
- `packages/dictionaries` (`mailroom_dictionaries`): 19 versioned vocabularies with a validating loader, version lock, source-value drift fixture, glossary (`docs/glossary.md`, generated).
- Tooling: root `pyproject.toml` (ruff, mypy strict for new packages, codespell), `Makefile` (`make help`), `.pre-commit-config.yaml`, `scripts/check_boundaries.py`, `check_layout.py`, `check_no_session_links.sh`, root `tests/`, CircleCI `lint` job, ADR-0007, README layout map.
- Two independent Opus review rounds on the first build found no blockers and 9 majors (torn-tail append, secret in error chain, redaction gaps, alias mismatch with the Dojo scorer, missing vocabularies, override rules, drift guard); all were fixed by two Opus fixers with regression tests.

## Not done

- Round 2 verification review of the fix commits (`git diff e25b829..HEAD`) was started and stopped to save usage. Worth one Opus reviewer, focusing on the new `flock` in `append_jsonl` (throughput), the redaction regexes (backtracking on 1 MB input), the NFKC fold, and a fresh `make install` in a copy.
- Open the PR ready for review (not draft), base = PR #2's branch until #2 merges, then retarget. Body: only the attribution HTML comment with the login, no project-thread link, no footers (strip right after create with `update_pull_request`). Fill from `.github/pull_request_template.md` if the governance PR has landed.
- CircleCI must be set up for the repo (JJB's to-do), then green on the final head; all reviews resolved; clean session-link sweep (`bash scripts/check_no_session_links.sh`); squash with a clean message, after #2 and #3.
- Known skipped on purpose: alias-expansion cap in config, `iter_jsonl` scalar-tail and CRLF-at-limit edge cases.

## Follow-ups for other threads (not done here)

- `packages/record`: import `mailroom_common.hashing` and adopt `jsonl`; add `py.typed`; fix 5 mypy strict errors and 9 unformatted files (ruff currently has per-file ignores for record); a real Python 3.11 bug at `packages/record/tests/test_record_machine.py:197` (backslash in an f-string); delete its duplicate `test_boundaries.py`; change record's row in `scripts/check_boundaries.py` to allow `common`.
- Labels and Dojo threads: read status words, field types and closed vocabularies from `mailroom_dictionaries` (both were told the import path); profile references vocabularies by name via `doc_classes.closed_label_fields`.
- Engine: map provider SDK errors onto `ProviderError` subclasses; Modal cold-start backoff is adapter-level.
- The human review desk (JJB, 2026-11-18 13:23 UTC) is a separate project: its own node plus a desk package, writing human corrections to the record. Not started.

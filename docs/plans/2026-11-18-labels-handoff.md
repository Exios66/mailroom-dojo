# Handoff: project 2 (labels package and EDA move)

Branch: `claude/project-thread-pbvxf9` (PR #5). PR #2 (record) is on `main` and was merged into the branch, so PR #5 is no longer stacked: it targets `main`. Plan: `docs/plans/2026-11-18-labels-plan.md`.

## State
- Library `packages/labels/` (`mailroom_labels`): snapshots with tamper-checked manifests, four label layers, resolver, quality gate, coverage, importers, CLI `mailroom-labels`, `LabelSet` for the Dojo.
- Committed data: `labels/hub-ccd040f6/`, `labels/interim-2026-11-01/`, `labels/near-gold-2026-11-17/`, `labels/gold-review-2026-11-18-r2/`; profile and known warnings in `config/clients/mailroom-dataset/`; reports in `docs/reports/labels/`.
- Last verified on head 03154aa (clean tree): `.venv/bin/python -m pytest packages -q -n 4` gave `628 passed` (record 267, labels 361); `mailroom-labels verify --strict --profile config/clients/mailroom-dataset/profile.yaml` on the four snapshots gave `0 error(s), 13 warning(s)` (all known); compileall exit 0. At that head no linter or type checker was configured; `main` now has `make lint` (ruff check and format check) and `make typecheck` (mypy `--strict` over the packages listed in `[tool.mypy] files`, which do not include `labels`), and the labels package was brought under the workspace ruff and codespell config (567ee02).
- Reviews: independent Opus review of Task 1 (9 findings fixed) and of the data and importers (data faithful to every source, my own resolver agreed on all 33,716 fields, 0 blocking). Its 3 majors and minors were fixed in the last nine commits.
- Final batch (`fb26826..03154aa`): round-3 review A1 to A7 (an `unsure` desk mark is listed under `source.undecided`), CodeAnt B1 to B8, C1 to C5, N1 to N10, and round-2 MINOR 1 (plan decision 4). New manifest/report facts: optional `source.undecided` (review), `text_unchecked` warning when a Hub manifest's `text_check.run` is not true, `config/clients/mailroom-dataset/hub-revisions.yaml` pins (`import-hub --pins`), `verify --strict` and `load_label_set(strict=True)`. The committed snapshots are byte-identical (`git diff fb26826..HEAD -- labels/` is empty); the coverage report's column headers were renamed.
- After the merge of `main` (9ab3a90): an `unsure` desk mark that cannot be ordered against a human decision on its label (undated, or the same instant as the latest decision) refuses the import (`source.unordered_undecided`); a later `unsure` withdraws the earlier human decisions on its label (194a4f2); `load_label_set` is strict by default (0532470); a rebuild is built aside and swapped in, so an error keeps the old snapshot (f18b13b); importers hash and parse the same bytes (928013a).

## Not done
- Independent reviews: done. The final fix range had a round-3 review; fresh pre-PR reviews ran on the landing heads 0532470 and 85e0853, and their code and docs findings are fixed on the landing branch.
- CircleCI is now configured on `main` (`.circleci/config.yml`); it must be green on the landing head before the merge.
- Not verified: live `fetch_hub.py` download, `build_profile.py` regeneration (needs the EDA clone), rebuilding the review snapshot from public sources (inputs are project files).
- Merge into `main` through the merge gate in `.claude/rules/pr-flow.md`, step 7.

## Open questions for the owner
1. Human desk decisions are `gold` with the desk role as reviewer (no reviewer initials recorded). Should initials be added first?
2. Six documents re-filed under another subclass between Hub revisions are unverified (53 fields unscored; the sandbox scores them). Rebuild interim on ccd040f6, or drop only their stale interim labels?
3. Review decision files live only in project files; move them into a repository?
4. A later `unsure` desk mark withdraws the earlier human decisions on its label. Is that the intended desk rule?
5. An `unsure` mark that cannot be ordered against a decision on its label refuses the whole import. Should it instead withdraw only that label?
6. The owner's initials were in 1,031 note fields of the imported review snapshot `gold-review-2026-11-18`. It is replaced on the landing branch by `gold-review-2026-11-18-r2`, derived from it by `packages/labels/scripts/derive_review_snapshot.py` with only those initials replaced by `review desk`, so they do not reach `main`. They remain in files already on `main`: `packages/record/tests/record_testkit.py`, the ADR "Deciders" lines, the record plan, the record test strategy, the shared-libraries hand-off and the design spec. Remove them?
7. DOC-672cec2c85c83c39 action_items: confirm the audit value replaces the catalog's empty list.
8. Claude audit decisions outrank interim `gap_fill` (47 of 69 change the value). Keep?

## Next
CircleCI green on the landing head, then merge. The Dojo thread codes against `load_label_set`. The review-desk node needs the review-decision shape in `labels/gold-review-2026-11-18-r2/`.

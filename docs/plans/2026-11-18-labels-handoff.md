# Handoff: project 2 (labels package and EDA move)

Branch: `claude/project-thread-pbvxf9` (pushed), stacked on `claude/project-thread-h3rj4z` (PR #2). Plan: `docs/plans/2026-11-18-labels-plan.md`.

## State
- Library `packages/labels/` (`mailroom_labels`): snapshots with tamper-checked manifests, four label layers, resolver, quality gate, coverage, importers, CLI `mailroom-labels`, `LabelSet` for the Dojo.
- Committed data: `labels/hub-ccd040f6/`, `labels/interim-2026-11-01/`, `labels/near-gold-2026-11-17/`, `labels/gold-review-2026-11-18/`; profile and known warnings in `config/clients/mailroom-dataset/`; reports in `docs/reports/labels/`.
- Last verified on head 03154aa (clean tree): `.venv/bin/python -m pytest packages -q -n 4` gave `628 passed` (record 267, labels 361); `mailroom-labels verify --strict --profile config/clients/mailroom-dataset/profile.yaml` on the four snapshots gave `0 error(s), 13 warning(s)` (all known); compileall exit 0. No linter or type checker is configured.
- Reviews: independent Opus review of Task 1 (9 findings fixed) and of the data and importers (data faithful to every source, my own resolver agreed on all 33,716 fields, 0 blocking). Its 3 majors and minors were fixed in the last nine commits.
- Final batch (`fb26826..03154aa`): round-3 review A1 to A7 (an `unsure` desk mark is listed under `source.undecided`, never refused), CodeAnt B1 to B8, C1 to C5, N1 to N10, and round-2 MINOR 1 (plan decision 4). New manifest/report facts: optional `source.undecided` (review), `text_unchecked` warning when a Hub manifest's `text_check.run` is not true, `config/clients/mailroom-dataset/hub-revisions.yaml` pins (`import-hub --pins`), `verify --strict` and `load_label_set(strict=True)`. The committed snapshots are byte-identical (`git diff fb26826..HEAD -- labels/` is empty); the coverage report's column headers were renamed.

## Not done
- No independent review of the final nine fix commits (`86f292d..156d48f`, i.e. 78cdede through 156d48f); one fresh Opus review of that range is the next step. The fixer was stopped before its final report, so its gate output was not seen; the head gate above was re-run by the lead.
- CircleCI has not run on this branch (project not set up for the repo).
- Not verified: live `fetch_hub.py` download, `build_profile.py` regeneration (needs the EDA clone), rebuilding the review snapshot from public sources (inputs are project files).
- Merge only after PRs #2 and #3 land, then retarget this PR to `main`.

## Open questions for JJB
1. Human desk decisions are `gold` with the desk role as reviewer (no initials recorded). Should initials be added first?
2. Six documents re-filed under another subclass between Hub revisions are unverified (53 fields unscored; the sandbox scores them). Rebuild interim on ccd040f6, or drop only their stale interim labels?
3. Review decision files live only in project files; move them into a repository?
4. DOC-672cec2c85c83c39 action_items: confirm the audit value replaces the catalog's empty list.
5. Claude audit decisions outrank interim `gap_fill` (47 of 69 change the value). Keep?

## Next
Review the final fix range, run CircleCI, then merge. The Dojo thread codes against `load_label_set`. The review-desk node needs the review-decision shape in `labels/gold-review-2026-11-18/`.

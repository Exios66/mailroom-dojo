# Port inventory: what still has to move into mailroom-dojo

Date: 2026-11-18. Read-only investigation for the orchestrator. Nothing in any repository was changed.

**Superseded where it differs.** `2026-11-18-workspace-port-design.md` wins over this inventory's verdicts,
build order and module names. Since this was written: design open question 2 is answered (the launcher floor
replaces The-Mailroom at cutover, so no span-name compatibility); `ui/` and `hosted/` are placed in the design's
section 7; every OPEN row is listed there for the owner to confirm; the sandbox and mailroom-reloaded sources
marked "source not available here" are covered by `2026-11-18-launcher-inventory.md`; record PR #2 and the
shared leaves PR #6 are merged to `main`.

## Sources and method

| Source | Commit | Python modules (tests excluded) | Lines |
|---|---|---:|---:|
| `llm-dojo-scoring` (`llm_dojo_scoring/`, 0.30.x) | `23f8234` | 84 | 31,450 |
| `llm-mailroom` (`src/`, incl. `src/scripts/`; pyproject version 0.9.3) | `070663f` | 214 | 80,124 |
| `The-Mailroom` (Python packages + `scripts/`; v0.5.1) | `40a0600` | 58 | 15,926 |
| Target `mailroom-dojo`, branch `claude/mailroom-dojo-workspace-port-ggh0x0` | `bb63cf8` | packages `record`, `labels`, `dojo` (scoring core), `common`, `dictionaries` | - |

- **Lines** are physical lines per file (`len(text.splitlines())`, blanks and comments included), from a scratch AST walker
  (`walk.py`, `maildeps.py`, `dojodeps.py`, `gen.py` in a scratch directory, not committed). Test trees are excluded; for scale: old Dojo
  `tests/` 113 files / 27,958 lines, llm-mailroom `src/tests` 212 files /
  63,976 lines, The-Mailroom `tests/` 53 files / 11,141 lines.
  The existing Dojo port inventory used `wc -l` too, so its line numbers match these (e.g. `field_scoring.py` 1957).
- **"What it does"** is the module docstring's first paragraph (blank where the module has none), then my note in italics.
- **"Depends on"** is the module's imports inside the same source tree, resolved by AST (top-level and lazy imports alike),
  minus modules whose verdict is PORTED already, DROP or REPLACED BY RECORD (those are not a porting prerequisite;
  for the old Dojo a dropped dependency is still listed with "(drop)" because its behaviour may need a replacement).
- **"Old tests naming it"** (old Dojo only): test files that import the module by dotted path or `from … import <name>`;
  package `__init__` rows are noisy and shown as "-".
- **Verdicts:** PORTED already / PORT AS IS / PORT AND DE-HARDCODE / BECOMES CONFIG / REPLACED BY RECORD / DROP (reason given),
  plus two I needed: **OPEN (not in design)** for capabilities the design neither keeps nor drops (an owner decision),
  and **REVIEW STATION (other agent)** for the human review surface, which another agent owns; those rows only say where it plugs in.
- Verdicts are mine, argued from the design (`docs/specs/2026-11-18-lean-rewrite-design.md`: "What is dropped", "The engine",
  "The launcher and backends", R4, R9, R10) and from the docstrings and imports. They are proposals for the chunk plans,
  not decisions. The rule I applied is the design's: "A module survives only if a gate, a report or a projection reads its output."

### Not in scope here, and where it plugs in

- **Run log storage (other agent).** On this branch `mailroom_record` has `blobs`, `hashing`, `log`, `models`, `seal`,
  `upgrade` (record plan Tasks 2 to 6). `verify`, `index`, `trace`, `versions`, `cli` exist on
  `origin/claude/project-thread-h3rj4z` (head `49e802f`, PR #3) and are **not merged into this branch**. The sandbox-run
  importer (Task 10), floor replay timeline (Task 10b), pack/prune (Task 11) and the stage-1 gate (Task 12) were not found on
  any local branch. Every Dojo chunk that reads runs (D5, D6, D7, the stage-2 gate) waits for Task 10; the floor (L4)
  waits for Task 10b.
- **Human review station (other agent).** Plug-in points found: llm-mailroom `pipeline/review_resolve.py` (909),
  `pipeline/review_priority.py` (139), `scripts/export_review_labels.py` (431), the review routes in `api/main.py`;
  The-Mailroom `mailroom_ui/review_actions.py` (834), `scripts/demo_review_tray.py` (320) and the `web/js/review*.js` front-end.
  The labels handoff says the desk node needs the review-decision shape in `labels/gold-review-2026-11-18/`.
- **Sources not available in this session:** `local-mailroom-sandbox` (needed for: the launcher's run spec, preflight,
  spend gate, price sheet check, Modal/Compose helpers, autoresearch driver, sandbox CLI and TUI; the Dojo's archive gate and
  cause codes, run metrics, specialist and grid cards, serving parity, routing audit and keep rule; the floor replay
  contract `tui/floor_replay.py:build_timeline`; the `doc_scorers` golden capture, which runs in a sandbox venv) and
  `mailroom-reloaded` (the TUI base, R7). Rows that need them are marked **source not available here**.

## Totals by verdict

### llm-dojo-scoring

| Verdict | Modules | Lines |
|---|---:|---:|
| PORT AND DE-HARDCODE | 20 | 12,260 |
| BECOMES CONFIG | 15 | 5,842 |
| DROP | 23 | 5,517 |
| PORTED already | 13 | 5,385 |
| PORT AS IS | 12 | 2,336 |
| REPLACED BY RECORD | 1 | 110 |

"PORTED already" counts whole modules of which chunk 1 took the parts it needed (`gt_metadata`, `corpus`, `equivalences`,
`intents`, `scored_enums`, `cuad_schema` names, `config`); their leftovers are assigned to D3a below.

### llm-mailroom

| Verdict | Modules | Lines |
|---|---:|---:|
| PORT AND DE-HARDCODE | 74 | 36,677 |
| DROP | 63 | 13,097 |
| REPLACED BY RECORD | 33 | 10,676 |
| OPEN (not in design) | 19 | 9,034 |
| BECOMES CONFIG | 9 | 5,290 |
| PORT AS IS | 13 | 3,871 |
| REVIEW STATION (other agent) | 3 | 1,479 |

### The-Mailroom (Python only; front-ends listed separately at the end of 1c)

| Verdict | Modules | Lines |
|---|---:|---:|
| REPLACED BY RECORD | 12 | 4,487 |
| PORT AND DE-HARDCODE | 16 | 4,302 |
| DROP | 19 | 3,851 |
| REVIEW STATION (other agent) | 2 | 1,154 |
| PORT AS IS | 6 | 1,123 |
| BECOMES CONFIG | 1 | 678 |
| OPEN (not in design) | 2 | 331 |

## 1. Module tables

### 1a. llm-dojo-scoring

Chunk ids refer to section 2. "done" = already in `packages/dojo` (see `docs/plans/2026-11-18-dojo-scoring-port-inventory.md`, not redone here).


#### `llm_dojo_scoring/` top level (3 modules, 1,423 lines)

| Module | Lines | What it does | Verdict | Chunk | Target | Depends on (not yet ported) | Old tests naming it |
|---|---:|---|---|---|---|---|---|
| `__init__.py` | 684 | llm-dojo-scoring — the scoring engine for LLM document pipelines. *0.30 public surface; design: clean break, no compatibility layer* | DROP | - | new `mailroom_dojo/__init__.py` | _layout (drop) | - |
| `_layout.py` | 113 | Package layout and the legacy flat import paths. *legacy flat import paths dropped by the design ("What is dropped")* | DROP | - | - | - | 5: test_consumer_compat.py, test_legacy_aliases_kept.py, test_package_layout.py … |
| `config.py` | 626 | Central, importable configuration for the dojo scoring suite. *replaced by immutable `ScoringProfile`; remaining knobs (CUAD subtypes, aliases) go with the schemas chunk* | PORTED already | done | `mailroom_dojo.profile` + `config/clients/mailroom-dataset/scoring.yaml` | - | 15: conftest.py, test_codeant_edge_regressions.py, test_consumer_compat.py … |

#### `llm_dojo_scoring/analysis` (8 modules, 2,938 lines)

| Module | Lines | What it does | Verdict | Chunk | Target | Depends on (not yet ported) | Old tests naming it |
|---|---:|---|---|---|---|---|---|
| `analysis/__init__.py` | 1 | Run analysis and reporting: statistics, diagnostics, interpretation, exports, grids, serving, cost. *empty package marker* | DROP | - | - | - | - |
| `analysis/bootstrap.py` | 186 | Bootstrap confidence intervals and small-sample delta testing. *paired bootstrap intervals for run comparison* | PORT AS IS | D1 | `mailroom_dojo/statistics/bootstrap.py` | - | 4: test_bootstrap.py, test_ml_contract.py, test_root_surface.py … |
| `analysis/calibration.py` | 396 | Calibration, risk-bound, and discrimination math shared by scoring lanes. *confidence layer: calibration error, risk bounds, ranking power* | PORT AS IS | D1 | `mailroom_dojo/statistics/calibration.py` | analysis/bootstrap | 5: test_calibration.py, test_ml_contract.py, test_pr104_review_fixes.py … |
| `analysis/cost.py` | 178 | Per-model token pricing, cost estimation, and usage aggregation. *cost truth layer; token prices are hard-coded tables today* | PORT AND DE-HARDCODE | D5 | `mailroom_dojo/run_layer/cost.py`; prices -> `config/prices/` (price sheet; D5 creates the folder with its layout-map line; launcher L6 and Dojo D7 cost truth read it) | metrics/scorecard_honesty (drop) | 5: test_codeant_cost_fnol.py, test_cost.py, test_serving.py … |
| `analysis/diagnostics.py` | 326 | Run-level diagnostic metrics for the entity-extraction task. *run-level extraction diagnostics* | PORT AS IS | D5 | `mailroom_dojo/run_layer/diagnostics.py` | - | 3: test_comparability.py, test_diagnostics.py, test_v026_truth_fixes.py |
| `analysis/experiment.py` | 110 | Experiment-log record helpers: JSONL append/load, dotted access, snapshots. *experiment-log JSONL helpers; the record is the experiment log* | REPLACED BY RECORD | - | - | - | 1: test_experiment.py |
| `analysis/failure_modes.py` | 184 | Failure-mode taxonomy and classification for sorter / subtype evals. *failure taxonomy for sorter/subtype; cause tokens from `mailroom_dictionaries` `review_causes`/`outcomes`* | PORT AND DE-HARDCODE | D5 | `mailroom_dojo/reports/failure_modes.py` | metrics/scorecard_honesty (drop) | 2: test_failure_modes.py, test_v018_scorecard_honesty.py |
| `analysis/serving.py` | 1557 | Local vs API serving comparison — TTFT, throughput, utilization, identity. *serving figures by backend (TTFT, throughput, GPU time); backend is a column, not a branch* | PORT AND DE-HARDCODE | D5 | `mailroom_dojo/run_layer/serving.py` | analysis/bootstrap, analysis/cost, metrics/emitter (drop), metrics/registry, metrics/scorecard_honesty (drop) | 8: test_codeant_cost_fnol.py, test_comparability.py, test_consumer_compat.py … |

#### `llm_dojo_scoring/integrations` (2 modules, 531 lines)

| Module | Lines | What it does | Verdict | Chunk | Target | Depends on (not yet ported) | Old tests naming it |
|---|---:|---|---|---|---|---|---|
| `integrations/__init__.py` | 1 | Trace sinks and release awareness: Langfuse, Phoenix, update checks. | DROP | - | - | - | - |
| `integrations/update_awareness.py` | 530 | Opt-in release awareness; importing Dojo and scoring never start checks. *release-awareness tool dropped by the design* | DROP | - | - | - | 1: test_update_awareness.py |

#### `llm_dojo_scoring/metrics` (10 modules, 5,418 lines)

| Module | Lines | What it does | Verdict | Chunk | Target | Depends on (not yet ported) | Old tests naming it |
|---|---:|---|---|---|---|---|---|
| `metrics/__init__.py` | 1 | Metric identity and emission: registry, metadata, bundles, profiles, emitter, honesty checks. | DROP | - | - | - | - |
| `metrics/bundles.py` | 387 | Pre-built metric bundles — named groupings of registry metrics per task type, so a new agent is ONE profile en… *tiers/bundles/profiles dropped: one flat metric table* | DROP | - | - | metrics/registry | 4: test_bundles.py, test_doc_bundles.py, test_profiles_registry.py … |
| `metrics/doc_bundles.py` | 282 | Document-type-aware metric bundles (KANBAN-067). *as bundles* | DROP | - | - | metrics/bundles (drop), metrics/registry | 2: test_doc_bundles.py, test_doc_classes.py |
| `metrics/emitter.py` | 449 | Unified score emitter — ONE routing layer for scores instead of each repo calling observability backends direc… *routes scores to trace backends; the Dojo writes reports, exporters feed from events* | DROP | - | - | metrics/registry, metrics/scorecard_honesty (drop), pipeline/mailroom (drop) | 5: test_emit_provenance.py, test_emitter.py, test_review_fixes_sandbox_238.py … |
| `metrics/metric_meta.py` | 607 | Citation / inclusion / ground-truth metadata for T0+T1 registry names. *citations / GT metadata per metric name* | BECOMES CONFIG | D2 | columns of the flat metric table (in configuration, loaded by `mailroom_dojo/measures/`) | - | 0 |
| `metrics/profiles.py` | 417 | Agent profile system — one declarative registration per agent instead of 4+ file edits across repos. *agent profiles dropped with bundles* | DROP | - | - | metrics/bundles (drop), metrics/doc_bundles (drop), metrics/registry | 8: test_bundles.py, test_doc_bundles.py, test_doc_classes.py … |
| `metrics/pruning.py` | 97 | Tier-based pruning — the dashboard-facing filter layer (KANBAN-061). *tier pruning dropped* | DROP | - | - | metrics/bundles (drop), metrics/profiles (drop), metrics/registry | 3: test_bundles.py, test_serving.py, test_t0_t1_headlines.py |
| `metrics/registry.py` | 1589 | Metric definitions registry — the single source mapping every score name to its tier, units, aggregation, and … *1,589 lines incl. tiers; keep names/units/aggregation only* | PORT AND DE-HARDCODE | D2 | `mailroom_dojo/measures/table.py` (one flat table: name, unit, aggregation, direction) | metrics/metric_meta | 17: test_bundles.py, test_consumer_compat.py, test_decision_lanes.py … |
| `metrics/score_changelog.py` | 413 | Machine-readable ledger of releases that moved a score (issue #97). *the machine ledger form is already in place for the scoring core; extend per chunk* | PORTED already | done | `packages/dojo/docs/score-changelog.md` + `intended_differences.json` | - | 3: test_comparability.py, test_ml_contract.py, test_pr104_review_fixes.py |
| `metrics/scorecard_honesty.py` | 1176 | v0.18 scorecard honesty — fail-closed GT, metric identity, provenance, completion. *fail-closed GT logic must survive as that rule; 11 modules import it* | DROP | D2 | replaced by the "every number carries n, cost basis, label status" rule in `mailroom_dojo/measures/` (`Measure`) | schemas/maud, scoring/suites | 22: test_comparability.py, test_correspondence_gt_audit.py, test_decision_lanes.py … |

#### `llm_dojo_scoring/ml` (3 modules, 310 lines)

| Module | Lines | What it does | Verdict | Chunk | Target | Depends on (not yet ported) | Old tests naming it |
|---|---:|---|---|---|---|---|---|
| `ml/__init__.py` | 21 | Contract and scoring for the trained intake classifier (``mailroom-ml``). *mailroom-ml contract stays on the 0.30 line* | DROP | - | - | ml/contract (drop), ml/metrics (drop) | - |
| `ml/contract.py` | 95 | The label and intake contract ``mailroom-ml`` vendors. *as above* | DROP | - | - | pipeline/intake | 1: test_ml_contract.py |
| `ml/metrics.py` | 194 | Score the trained intake classifier against dataset gold. *as above* | DROP | - | - | analysis/bootstrap, analysis/calibration | 34: test_bundles.py, test_comparability.py, test_consumer_compat.py … |

#### `llm_dojo_scoring/pipeline` (10 modules, 3,337 lines)

| Module | Lines | What it does | Verdict | Chunk | Target | Depends on (not yet ported) | Old tests naming it |
|---|---:|---|---|---|---|---|---|
| `pipeline/__init__.py` | 1 | Mailroom pipeline and sandbox contracts: intake, pilot labels, verdicts, archive, sandbox rubrics. | DROP | - | - | - | - |
| `pipeline/archive.py` | 1153 | Archivist scoring block and hash-chained ``archived`` audit row. *document outcome layer; the sandbox archive gate and cause code are the stated source (source not available here)* | PORT AND DE-HARDCODE | D6 | scoring block -> `mailroom_dojo/outcome/archive_gate.py`; hash-chained audit row -> REPLACED BY RECORD | metrics/scorecard_honesty (drop), pipeline/sandbox_schema_adherence, schemas/label_policy, scoring/doc_scorers/common, scoring/doc_scorers/dispatch, scoring/extraction_metrics | 11: test_archive_scoring.py, test_bug_sweep_fixes.py, test_consumer_compat.py … |
| `pipeline/intake.py` | 417 | Intake clerk — pre-sorter text cleaning, format fixes, and prep. *intake clerk text cleaning is pipeline code living in the Dojo; llm-mailroom `agents/intake.py` imports it* | PORT AS IS | E (engine/config) | engine: `mailroom_engine/agents/intake_cleaning.py` | - | 4: test_codeant_edge_regressions.py, test_intake.py, test_ml_contract.py … |
| `pipeline/mailroom.py` | 318 | Live LLM-Mailroom / The-Mailroom pipeline contract. *pipeline contract (aliases, node names); class aliases already profile data* | DROP | - | - | pipeline/intake | 8: test_archive_scoring.py, test_consumer_compat.py, test_doc_classes.py … |
| `pipeline/pilot_labels.py` | 172 | Exact pipeline label policies, separate from public generic task defaults. *exact pipeline label policies* | PORT AND DE-HARDCODE | D3b | `mailroom_dojo/classification/` (exact pipeline label policy as profile rules) | - | 4: test_codeant_cost_fnol.py, test_codeant_edge_regressions.py, test_consumer_compat.py … |
| `pipeline/pilot_summary.py` | 51 | Pilot classification support; display/usage aggregation stays producer-local. *display aggregation, producer-local* | DROP | - | - | pipeline/pipeline_scoring | 2: test_codeant_edge_regressions.py, test_final_quality_transfer.py |
| `pipeline/pipeline_scoring.py` | 202 | Exact pipeline classification and offline lifecycle reports. *pipeline classification and lifecycle reports* | PORT AS IS | D5 | `mailroom_dojo/run_layer/classification_report.py` | - | 2: test_core_engine_transfer.py, test_final_quality_transfer.py |
| `pipeline/pipeline_verdict.py` | 35 | Exact deterministic pipeline verdict thresholds, independent of config I/O. *small threshold table* | BECOMES CONFIG | D6 | `config/policies/legacy.*` thresholds (engine reads) + Dojo outcome bar in `scoring.yaml` | - | 1: test_residual_engine_transfer.py |
| `pipeline/sandbox_schema_adherence.py` | 497 | Sandbox payload-quality policies: parsing, schema adherence and floor modes. *schema adherence + floor modes; launcher hands this to the Dojo* | PORT AND DE-HARDCODE | D3b | `mailroom_dojo/run_layer/schema_adherence.py` | schemas/corporate_record_schema | 9: test_codeant_edge_regressions.py, test_doc_classes.py, test_residual_engine_transfer.py … |
| `pipeline/sandbox_scoring.py` | 491 | Dojo-owned sandbox MAUD/CUAD benchmark rubrics, additive to current suites. *needed for mergers (Set E) and contracts* | PORT AND DE-HARDCODE | D3b | `mailroom_dojo/field_scoring/rubrics/` (MAUD/CUAD benchmark rubrics) | metrics/registry, metrics/scorecard_honesty (drop), schemas/maud | 4: test_codeant_edge_regressions.py, test_cuad_date_fields.py, test_sandbox_scoring_adoption.py … |

#### `llm_dojo_scoring/prompts` (3 modules, 526 lines)

| Module | Lines | What it does | Verdict | Chunk | Target | Depends on (not yet ported) | Old tests naming it |
|---|---:|---|---|---|---|---|---|
| `prompts/__init__.py` | 187 | Importable prompt catalog for llm-dojo-scoring. *design: prompt catalog moves to the engine* | BECOMES CONFIG | E (engine/config) | `config/prompts/` (catalog) read by the engine | prompts/lineage | - |
| `prompts/alignment.py` | 84 | Do the specialist prompts ask for the fields the scoring schema scores? *"each prompt names every field and allowed value" check moves to the engine tests* | PORT AND DE-HARDCODE | E (engine/config) | interim: the workspace `tests/` (P1, until the engine exists); then engine tests: `packages/engine/tests/test_prompt_alignment.py` (E2). Compares each prompt with its schema's `extracted` section; `legacy` gaps against the `scored` section are recorded as known gaps, never closed by editing a `legacy` prompt | - | 2: test_production_prompts.py, test_prompt_alignment.py |
| `prompts/lineage.py` | 255 | eval-environment prompt lineage: every archived, frozen and mutated version. *eval-environment lineage; data, no code* | BECOMES CONFIG | E (engine/config) | `config/prompts/frozen_v1/lineage.json` (data only) | - | 0 |

#### `llm_dojo_scoring/schemas` (18 modules, 7,624 lines)

| Module | Lines | What it does | Verdict | Chunk | Target | Depends on (not yet ported) | Old tests naming it |
|---|---:|---|---|---|---|---|---|
| `schemas/__init__.py` | 1 | Dataset, corpus and ground-truth contracts: what the mailroom-dataset rows mean. | DROP | - | - | - | - |
| `schemas/corporate_record_relabel.py` | 183 | Deterministic repair of ``corporate_record`` ground-truth rows. *deterministic GT repair; the labels package carries reviewed overlays. Verify value parity before dropping* | DROP | D4 | (superseded by `labels/gold-review-*` overlays; verify) | schemas/corporate_record_schema | 1: test_corporate_record_relabel.py |
| `schemas/corporate_record_schema.py` | 734 | Corporate-record entity schema — one contract per record subclass. *one contract per record subclass* | BECOMES CONFIG | D3a | `config/clients/mailroom-dataset/schemas/corporate_record.yaml` + generic loader `mailroom_dojo/schemas/` | - | 6: test_corporate_record_relabel.py, test_corporate_record_schema.py, test_corporate_record_scripts.py … |
| `schemas/corpus.py` | 525 | mailroom-dataset corpus alignment — schemas, subclasses, differentiators. *module dropped in chunk 1; differentiators and subclass tables remain for sorter guide* | PORTED already | done | constants already in `profile.yaml`/`scoring.yaml`; differentiators -> sorter guide config | - | 17: test_content_scoring.py, test_corporate_record_schema.py, test_corpus.py … |
| `schemas/correspondence_gt_audit.py` | 346 | Audit correspondence ground truth for templated and metadata-derived labels (#94). *templated/metadata-derived label audit; label_policy imports it* | DROP | D4 | (superseded by labels quality gate; verify) | - | 1: test_correspondence_gt_audit.py |
| `schemas/cuad_schema.py` | 1150 | CUAD contract entity-extraction schema — categories, families, questions. *chunk 1 took only 2 names* | BECOMES CONFIG | D3a | `config/clients/mailroom-dataset/schemas/contract.yaml` (categories, families, questions); `cuad_family` vocabulary already in `mailroom_dictionaries` | - | 4: test_cuad_schema.py, test_doc_classes.py, test_scoring_fixes_127.py … |
| `schemas/doc_classes.py` | 335 | The document-class registry: the one table a person edits to change a class or a field. *class registry* | PORTED already | done | `config/clients/mailroom-dataset/profile.yaml` (labels) + `scoring.yaml` (Dojo) | - | 8: test_class_headline_contract.py, test_doc_classes.py, test_extraction_schemas.py … |
| `schemas/equivalences.py` | 76 | Subtype / doc-subclass normalization and equivalence helpers. *predicates used by docclass tasks* | PORTED already | done | folds in `mailroom_dojo/profile/label_folding.py`; equivalence predicates -> D3 | - | 3: test_cuad_schema.py, test_equivalences.py, test_final_quality_transfer.py |
| `schemas/extraction_schemas.py` | 583 | Extraction schemas for the five mailroom classes, per document type and subtype. *five classes per type/subtype; engine specialists read the same files* | BECOMES CONFIG | D3a | `config/clients/mailroom-dataset/schemas/<class>.yaml` + loader | schemas/corporate_record_schema, schemas/cuad_schema, schemas/maud_schema | 4: test_build_extraction_status.py, test_doc_classes.py, test_extraction_schemas.py … |
| `schemas/gt_metadata.py` | 344 | Parse mailroom-dataset ground-truth metadata into scoring-ready fields. *remaining CUAD presence helpers move with D3* | PORTED already | done | value helpers in `mailroom_dojo/values/`; Hub shaping in `labels_interface/hub_rows.py` | - | 6: test_bug_sweep_fixes.py, test_content_scoring.py, test_field_scoring.py … |
| `schemas/intents.py` | 224 | Controlled ``intent`` vocabularies for the three classes that carry one. *descriptions go to prompts (engine)* | PORTED already | done | fold algorithm ported; vocabularies in `mailroom_dictionaries` `*_intent.yaml` | - | 2: test_production_prompts.py, test_scored_enums.py |
| `schemas/label_policy.py` | 400 | What the Dojo scores against: Hub GT without its stale labels, plus the reviewed overlays. *design: label policy and scorable-field rules stay in the Dojo, reading the labels package* | PORT AND DE-HARDCODE | D4 | split: precedence/stale -> already `mailroom_labels.resolve`/`quality` (verify parity); scorable-field rule -> `mailroom_dojo/label_layer/` | schemas/corporate_record_schema, schemas/correspondence_gt_audit (drop) | 1: test_label_policy.py |
| `schemas/maud.py` | 530 | MAUD merger-agreement answer-class catalogs — no guessing on GT labels. *answer-class catalogs; merger work (project 4) confirms the 12-question clean subset* | BECOMES CONFIG | D3a | `config/clients/mailroom-dataset/schemas/maud_answers.yaml`; `merger_consideration` already a dictionary | - | 2: test_maud_catalog.py, test_maud_schema.py |
| `schemas/maud_schema.py` | 1323 | MAUD entity-extraction schema — the machine-checkable question→answer contract. *question -> answer contract; coverage and precision kept apart (design, Mergers)* | BECOMES CONFIG | D3a | `config/clients/mailroom-dataset/schemas/merger_agreement.yaml` + per-question scorer in `mailroom_dojo/field_scoring/maud_questions.py` | metrics/scorecard_honesty (drop), schemas/maud | 4: test_doc_classes.py, test_maud_schema.py, test_merger_maud_gold.py … |
| `schemas/schema_contract.py` | 106 | Empty-field contract guard for specialist extraction schemas. *empty-field guard* | PORT AS IS | D3a | `mailroom_dojo/schemas/contract_guard.py` | - | 2: test_doc_classes.py, test_schema_contract.py |
| `schemas/scored_enums.py` | 268 | Consumer-facing grammar hints for the enum fields the dojo scores (issue #96). | PORTED already | done | fold ported; prompt-hint helpers dropped (prompts are engine) | - | 6: test_bug_sweep_fixes.py, test_build_extraction_status.py, test_pr104_review_fixes.py … |
| `schemas/sorter_guide.py` | 235 | Sorter-facing subclass guide: what each subclass key means in the mailroom-dataset corpus. *subclass descriptions* | BECOMES CONFIG | D3a | `config/clients/mailroom-dataset/sorter_guide.yaml` (engine prompt input; Dojo failure modes read it) | - | 1: test_sorter_guide.py |
| `schemas/v10_rows.py` | 261 | Read mailroom-dataset v10 typed rows with the scorers built for ``gt_fields``. *reads v10 typed rows* | PORT AND DE-HARDCODE | D3a | labels importer side: `mailroom_labels.importers.hub` already reads v10 rows (verify); typed-row scoring glue -> D3 | - | 3: test_doc_classes.py, test_scoring_fixes_127.py, test_v10_rows.py |

#### `llm_dojo_scoring/scoring` (27 modules, 9,343 lines)

| Module | Lines | What it does | Verdict | Chunk | Target | Depends on (not yet ported) | Old tests naming it |
|---|---:|---|---|---|---|---|---|
| `scoring/__init__.py` | 1 | Deterministic scorers: fields, classification, content and task suites. | DROP | - | - | - | - |
| `scoring/_paired.py` | 9 | Cardinality validation for scorers consuming paired document sequences. | PORTED already | done | `mailroom_dojo/classification/paired.py` | - | 0 |
| `scoring/agent_rubrics.py` | 70 | Small dependency-free isolation-eval rubrics; no model invocation or I/O. *isolation-eval rubrics, dependency free* | PORT AS IS | D3b | `mailroom_dojo/suites/rubrics.py` | - | 1: test_core_engine_transfer.py |
| `scoring/asr.py` | 125 | Word- and character-error rates for PDF transcription / OCR. *WER/CER for transcriber; golden task kinds `wer`, `cer`, `transcription`* | PORT AS IS | D3b | `mailroom_dojo/tasks/transcription.py` | - | 2: test_asr.py, test_paired_cardinality.py |
| `scoring/claims_consistency.py` | 106 | Insurance determination-consistency and amount-exactness scorers. *insurance-claim tier-1 extra as a plug-in* | PORT AND DE-HARDCODE | D3b | `mailroom_dojo/field_scoring/plugins/claims_consistency.py` | - | 2: test_comparability.py, test_t0_t1_headlines.py |
| `scoring/classification.py` | 321 | Classification scorers: normalization, exact match, per-class stats, confusion matrices, and binary metrics. | PORTED already | done | `mailroom_dojo/classification/` | - | 3: test_classification.py, test_paired_cardinality.py, test_t0_t1_headlines.py |
| `scoring/content_scoring.py` | 807 | Enron topic/sentiment and MAUD per-question extraction scorers. *Enron topic/sentiment and MAUD per-question scorers* | PORT AND DE-HARDCODE | D3b | `mailroom_dojo/field_scoring/content.py` (topic/sentiment) + MAUD per-question | metrics/scorecard_honesty (drop), schemas/maud, scoring/tasks | 7: test_content_scoring.py, test_maud_catalog.py, test_maud_schema.py … |
| `scoring/decision_lanes.py` | 764 | Decision-lane scoring: judge, arbiter, boss and the human-review tray. *judge/arbiter/boss/review-tray scoring -> behaviour layer from decision events* | PORT AND DE-HARDCODE | D6 | `mailroom_dojo/behaviour/decision_lanes.py` | metrics/scorecard_honesty (drop) | 2: test_decision_lanes.py, test_v030_followups.py |
| `scoring/doc_scorers/__init__.py` | 50 | Per-document-type scorers: tier 0 (common), tier 1 (type) and tier 2 (subclass). *tier 0/1/2 per-document scorers* | PORT AND DE-HARDCODE | D3b | `mailroom_dojo/document_scoring/` registry (plug-in per class from profile) | scoring/doc_scorers/base, scoring/doc_scorers/contract, scoring/doc_scorers/corporate_record, scoring/doc_scorers/correspondence, scoring/doc_scorers/generic, scoring/doc_scorers/insurance_claim, scoring/doc_scorers/merger_agreement, scoring/suites | - |
| `scoring/doc_scorers/base.py` | 103 | The ``DocScorer`` protocol and a base class for per-document-type scorers. *`DocScorer` protocol* | PORT AS IS | D3b | `mailroom_dojo/document_scoring/base.py` | - | 0 |
| `scoring/doc_scorers/common.py` | 135 | Tier 0 of document scoring: what every document gets, whatever its type. *tier 0* | PORT AS IS | D3b | `mailroom_dojo/document_scoring/common.py` | scoring/doc_scorers/base, scoring/extraction_metrics, scoring/extraction_scope, scoring/suites | 0 |
| `scoring/doc_scorers/contract.py` | 43 | ``contract``: CUAD clause detection replaces the headline when CUAD labels exist. *class module -> profile entry + plug-in* | BECOMES CONFIG | D3b | plug-in registered by profile | pipeline/sandbox_scoring, scoring/doc_scorers/base | 1: test_ml_contract.py |
| `scoring/doc_scorers/corporate_record.py` | 17 | ``corporate_record``: always the detailed, per-record-type suite path. | BECOMES CONFIG | D3b | profile entry | scoring/doc_scorers/base | 0 |
| `scoring/doc_scorers/correspondence.py` | 57 | ``correspondence``: Enron topic and sentiment extras (tier 1) and subclass normalisation (tier 2). | BECOMES CONFIG | D3b | profile entry + content plug-in | scoring/content_scoring, scoring/doc_scorers/base, scoring/suites | 0 |
| `scoring/doc_scorers/dispatch.py` | 131 | Compose the scoring tiers for one document; ``score_document`` is the public entry. *R4: dispatch by profile, not by class module* | PORT AND DE-HARDCODE | D3b | `mailroom_dojo/document_scoring/dispatch.py` (`score_document`) | pipeline/sandbox_schema_adherence, scoring/doc_scorers/base, scoring/doc_scorers/common, scoring/extraction_scope | 0 |
| `scoring/doc_scorers/generic.py` | 11 | ``generic``: the scorer for types with no module of their own. *default for new types (R4)* | PORT AS IS | D3b | `mailroom_dojo/document_scoring/generic.py` | scoring/doc_scorers/base | 0 |
| `scoring/doc_scorers/insurance_claim.py` | 43 | ``insurance_claim``: claims consistency extras (tier 1) and the CMS source table (tier 2). | BECOMES CONFIG | D3b | profile entry + claims plug-in | scoring/claims_consistency, scoring/doc_scorers/base, scoring/extraction_scope | 0 |
| `scoring/doc_scorers/merger_agreement.py` | 43 | ``merger_agreement``: MAUD answers replace the headline when MAUD labels exist. | BECOMES CONFIG | D3b | profile entry + MAUD plug-in | pipeline/sandbox_scoring, scoring/doc_scorers/base | 0 |
| `scoring/extraction_metrics.py` | 259 | Field-micro precision / recall / F1 / F2 over extraction events. *field-micro P/R/F1/F2* | PORT AS IS | D3b | `mailroom_dojo/field_scoring/micro_metrics.py` | - | 6: test_class_headline_contract.py, test_gt_metadata.py, test_scoring_fixes_127.py … |
| `scoring/extraction_scope.py` | 189 | Scope extraction GT to the live class schema before scoring (SAND-026). *scope GT to the live class schema (reads schema config)* | PORT AND DE-HARDCODE | D3b | `mailroom_dojo/field_scoring/scope.py` | schemas/corporate_record_schema, schemas/label_policy | 4: test_doc_classes.py, test_doc_scorers_common.py, test_label_policy.py … |
| `scoring/field_scoring.py` | 1957 | Deterministic, field-type-aware extraction scoring. *embedding rescue kept as injected backend* | PORTED already | done | `mailroom_dojo/field_scoring/` | pipeline/mailroom (drop), schemas/cuad_schema | 21: test_bug_sweep_fixes.py, test_class_headline_contract.py, test_consumer_compat.py … |
| `scoring/field_support.py` | 48 | Exact GT support summaries for pipeline pilot and factuality adapters. | PORTED already | done | `field_audit_support` ported; pilot helpers dropped | - | 1: test_final_quality_transfer.py |
| `scoring/legalbench_scoring.py` | 168 | Deterministic scoring for LegalBench runs — every number computed locally. *LegalBench scoring dropped by the design* | DROP | - | - | pipeline/pipeline_scoring | 2: test_codeant_edge_regressions.py, test_core_engine_transfer.py |
| `scoring/reviewer.py` | 1030 | Reviewer-lane scoring for LLM and decision-model reviewers. *reviewer-lane scoring for LLM and decision-model reviewers (Set A)* | PORT AND DE-HARDCODE | D6 | `mailroom_dojo/behaviour/reviewer.py` | analysis/bootstrap, analysis/calibration, metrics/scorecard_honesty (drop), schemas/sorter_guide | 2: test_pr104_review_fixes.py, test_reviewer_scoring.py |
| `scoring/suites.py` | 1903 | Dedicated per-agent scoring suites. *1,903 lines; imports bundles/profiles/pruning/serving which are dropped or later* | PORT AND DE-HARDCODE | D3b | `mailroom_dojo/suites/` (one suite per agent role, metric names from the flat table) | analysis/serving, metrics/bundles (drop), metrics/profiles (drop), metrics/pruning (drop), metrics/registry, metrics/scorecard_honesty (drop), pipeline/intake, pipeline/mailroom (drop), schemas/corporate_record_schema, schemas/extraction_schemas, schemas/maud_schema, scoring/asr, scoring/claims_consistency, scoring/content_scoring, scoring/extraction_metrics, scoring/tasks | 33: test_archive_scoring.py, test_asr.py, test_audit_consumer_surface.py … |
| `scoring/tasks.py` | 714 | Task-aware scoring across the additional document hierarchy. *task kinds (22 in golden)* | PORT AND DE-HARDCODE | D3b | `mailroom_dojo/tasks/kinds.py` | analysis/bootstrap, pipeline/intake, pipeline/mailroom (drop), schemas/sorter_guide, scoring/asr, scoring/content_scoring | 11: test_asr.py, test_chained_policy.py, test_consumer_closure.py … |
| `scoring/trace_knobs.py` | 239 | Confidence and reasoning as captured experimental knobs. | PORTED already | done | `mailroom_dojo/field_scoring/trace_capture.py` | - | 1: test_trace_knobs.py |

#### Old Dojo `scripts/` (not modules; lines per file)

| Script | Lines | Verdict |
|---|---:|---|
| `freeze_goldens.py` | 400 | PORT AND DE-HARDCODE as the D3b parity harness (`packages/dojo/scripts/`, capture only, never in CI, as chunk 1 did) |
| `capture_sandbox_goldens.py` | 136 | Keep its output (`tests/fixtures/doc_scorers/goldens.json`); re-running it needs a sandbox venv: source not available here |
| `record_doc_scorer_departures.py` | 124 | PORT with D3b (departures overlay -> `intended_differences.json`) |
| `gen_maud_catalog.py` | 445 | PORT with D3a (generates the MAUD answer config) |
| `gen_taxonomy_fixture.py` | 107 | DROP (chunk 1 froze `old_registry_field_types.json`) |
| `build_extraction_status.py` | 318 | OPEN: status page; the labels coverage report may cover it |
| `render_gt_coverage.py` | 249 | REPLACED by `mailroom-labels` coverage report |
| `audit_corporate_record_labels.py`, `audit_correspondence_gt.py`, `relabel_corporate_record_gt.py`, `verify_gt_penalties.py` | 131, 105, 58, 306 | Label work: check against `mailroom_labels` quality gate and review overlays (D4) |
| `render_prompt_changelog.py`, `sync_production_prompts.py`, `sync_eval_environment_lineage.py` | 128, 209, 401 | Prompt catalog moves to the engine/config; sync scripts DROP (no cross-repo mirroring) |
| `release.py`, `audit_consumer_surface.py`, `consumer_closure.py`, `export_ml_contract.py`, `ci.py`, `t3-setup.sh` | 1842, 1181, 1166, 52, 84, 35 | DROP (0.30 release chain, consumer pins, ML contract; workspace has `Makefile` and its own CI) |

### 1b. llm-mailroom (`src/`)

Targets in `mailroom_engine` / `mailroom_launcher` are proposed paths; neither package exists yet.


#### `src/agents` (23 modules, 4,564 lines)

| Module | Lines | What it does | Verdict | Target | Depends on (to port first) | Third-party |
|---|---:|---|---|---|---|---|
| `agents/__init__.py` | 3 |  | DROP | - | - | - |
| `agents/arbiter.py` | 354 | Arbiter agent — Lane B judgment arbitration (KANBAN-063). *Lane B arbitration; model via client profile* | PORT AND DE-HARDCODE | `mailroom_engine/agents/arbiter.py` | agents/base, agents/source_windows, llm/prompt_doctrine, llm/prompts | structlog |
| `agents/archivist.py` | 82 |  *procedural; writes the archived blob through the record* | PORT AND DE-HARDCODE | `mailroom_engine/agents/archivist.py` | - | structlog |
| `agents/base.py` | 403 |  *`_call_structured` JSON boilerplate kept; one model client; records prompt id+hash* | PORT AND DE-HARDCODE | `mailroom_engine/agents/base.py` | langchain_agents/skills, langchain_agents/specialist_agents, llm/client, llm/retry, llm/vision, observability/tracing (+2 more) | structlog |
| `agents/bert_intake.py` | 268 | BERT intake fast-path lane (mailroom-issues #85 M6a / #98). *mailroom-ml fast path, off by default; ML contract stays on 0.30* | DROP | - | - | mailroom_ml, structlog |
| `agents/boss.py` | 129 |  | PORT AND DE-HARDCODE | `mailroom_engine/agents/boss.py` | agents/base, llm/prompt_doctrine, llm/prompts | structlog |
| `agents/contracts_specialist.py` | 69 | Contracts specialist — LangChain version vendored from llm-entity-extraction. *R4: specialists collapse into one schema-driven class* | PORT AND DE-HARDCODE | `mailroom_engine/agents/specialist.py` (one generic specialist + schema) | langchain_agents/prompts, langchain_agents/specialist_agents, llm/prompts, pipeline/config | structlog |
| `agents/corporate_records_specialist.py` | 73 |  *as above* | PORT AND DE-HARDCODE | `mailroom_engine/agents/specialist.py` | agents/base, langchain_agents/specialist_agents, llm/prompts | structlog |
| `agents/correspondence_specialist.py` | 79 |  *as above* | PORT AND DE-HARDCODE | `mailroom_engine/agents/specialist.py` | agents/base, langchain_agents/specialist_agents, llm/prompts, pipeline/extraction_normalize | structlog |
| `agents/decision_backend.py` | 29 | Registry for the decision-model reviewer backend. *decision-slot backend registry (R10)* | PORT AND DE-HARDCODE | `mailroom_engine/decisions/backends.py` | pipeline/decision_review | - |
| `agents/extraction_gate.py` | 65 | Registry and handoff contract for the extraction-gate reviewer. *slot handoff contract* | PORT AND DE-HARDCODE | `mailroom_engine/decisions/extraction_gate.py` | - | - |
| `agents/gmail_triage.py` | 619 | Gmail intake triage agent — free-model single-document lane (HUB-037). *triage is a design agent; Gmail channel itself is OPEN* | PORT AND DE-HARDCODE | `mailroom_engine/agents/triage.py` | agents/base, llm/prompts, llm/quota, pipeline/config, schemas/documents | structlog |
| `agents/image_extractor.py` | 116 | Image extraction agent — uses vision-capable LLMs to extract text from images. *procedural* | PORT AS IS | `mailroom_engine/agents/image_extractor.py` | agents/base, llm/prompt_doctrine, llm/prompts, llm/retry, observability/tracing, pipeline/limits | structlog |
| `agents/insurance_claims_specialist.py` | 84 |  | PORT AND DE-HARDCODE | `mailroom_engine/agents/specialist.py` | agents/base, langchain_agents/specialist_agents, llm/prompts, pipeline/extraction_normalize | structlog |
| `agents/intake.py` | 546 | Intake agent — triage + clean + prepare for the full pipeline (HUB-038). *imports `llm_dojo_scoring.pipeline.intake` today* | PORT AND DE-HARDCODE | `mailroom_engine/agents/intake.py` | agents/base, agents/gmail_triage, llm/prompts, observability/tracing, pipeline/config, pipeline/text_cleaning | llm_dojo_scoring |
| `agents/judge.py` | 534 | LLM-as-a-judge that evaluates extraction completeness against the source document. Used offline by s… | PORT AND DE-HARDCODE | `mailroom_engine/agents/judge.py` | agents/base, agents/source_windows, llm/prompt_doctrine, llm/prompts, pipeline/config, schemas/documents | structlog |
| `agents/merger_agreement_specialist.py` | 100 | Merger-agreement specialist — dedicated MAUD extract class. *merger per-question pass is a Set E experiment* | PORT AND DE-HARDCODE | `mailroom_engine/agents/specialist.py` + merger policy | langchain_agents/specialist_agents, llm/prompts, pipeline/config | structlog |
| `agents/pdf_transcriber.py` | 200 | PDF transcription agent — converts PDF documents to markdown for downstream agents. *procedural* | PORT AS IS | `mailroom_engine/agents/transcriber.py` | agents/base, llm/prompt_doctrine, llm/prompts, llm/retry, llm/vision, observability/tracing (+2 more) | pdfplumber, pypdf, structlog |
| `agents/relations.py` | 202 | Relations agent — LLM judgment pass over relation candidates (HUB-040). | PORT AND DE-HARDCODE | `mailroom_engine/agents/relations.py` | agents/base, llm/prompts | structlog |
| `agents/reporter.py` | 103 | Procedural matter-record assembler (reporter LLM retired). *procedural* | PORT AS IS | `mailroom_engine/agents/reporter.py` | - | structlog |
| `agents/sorter.py` | 226 | Sorter agent — LangChain version vendored from llm-entity-extraction. *merge with `langchain_agents/sorter_agent`* | PORT AND DE-HARDCODE | `mailroom_engine/agents/sorter.py` | agents/intake, langchain_agents/prompts, langchain_agents/sorter_agent, llm/prompts, pipeline/config | structlog |
| `agents/sorter_reviewer.py` | 126 | Sorter Reviewer agent — Lane A second-opinion classification (KANBAN-062). *decision slot "sorter reviewer"* | PORT AND DE-HARDCODE | `mailroom_engine/agents/sorter_reviewer.py` | agents/base, langchain_agents/doc_inventories, langchain_agents/sorter_agent, llm/prompt_doctrine, llm/prompts, pipeline/config | structlog |
| `agents/source_windows.py` | 154 | Whole-document coverage for the judge and the arbiters (#277). *sliding windows, never truncate* | PORT AS IS | `mailroom_engine/windows.py` | agents/intake, pipeline/config | - |

#### `src/api` (3 modules, 1,660 lines)

| Module | Lines | What it does | Verdict | Target | Depends on (to port first) | Third-party |
|---|---:|---|---|---|---|---|
| `api/__init__.py` | 3 |  | DROP | - | - | - |
| `api/auth.py` | 119 | Bearer/loopback authorization shared by every Mailroom HTTP app. | PORT AS IS | `mailroom_engine/api/auth.py` | - | fastapi |
| `api/main.py` | 1538 |  *1,538 lines; diagnostics/dojo routes drop; review routes plug into the review station* | PORT AND DE-HARDCODE | `mailroom_engine/api/app.py` (upload + review-resolve keep shape) | api/auth, graph/build_graph, graph/decision_points, graph/state, llm/client, observability/tracing (+9 more) | fastapi, llm_dojo_scoring, mailroom_diagnostics, openai, pydantic, structlog, uvicorn |

#### `src/audit` (19 modules, 4,767 lines)

| Module | Lines | What it does | Verdict | Target | Depends on (to port first) | Third-party |
|---|---:|---|---|---|---|---|
| `audit/__init__.py` | 1 | Unified audit row writers (spec §4–§6). | REPLACED BY RECORD | - | - | - |
| `audit/digests.py` | 52 | Content digests shared by the audit writers, the trace sinks and the verifier (spec §5.5, §5.6, §6). | REPLACED BY RECORD | `mailroom_record.hashing` | - | - |
| `audit/events.py` | 77 | Events on a document outside its run: an addendum after the row, a trace visit before it (spec §4.3)… | REPLACED BY RECORD | record events | graph/build_graph | structlog |
| `audit/legacy_events.py` | 82 | Per-stage audit entries for a database the unified audit has not migrated (spec §4.5, rollout). | REPLACED BY RECORD | - | - | structlog |
| `audit/metrics.py` | 186 | ``audit_row_metrics``: one flat record per document from its unified row (spec §7). | REPLACED BY RECORD | Dojo run layer reads the record | - | - |
| `audit/models.py` | 94 | Value types for the unified audit row writers (spec §4, roadmap M3). | REPLACED BY RECORD | `mailroom_record.models` | - | - |
| `audit/readers.py` | 217 | Readers over the unified audit row (spec §7): ``GET /audit/{doc_id}`` and ``GET /audit``. | REPLACED BY RECORD | `mailroom_record` trace (`trace run`/`trace doc`) | - | structlog |
| `audit/reconcile.py` | 561 | ``audit-reconcile``: write the rows that waited in the trace sinks (spec §5.6 step 3). *sinks are write-only exporters; nothing reconciles from them* | REPLACED BY RECORD | - | - | sqlalchemy, structlog |
| `audit/report.py` | 151 | ``audit-report``: one Markdown section per live document type (spec §7, #235 acceptance 3). | REPLACED BY RECORD | Dojo report | pipeline/config | - |
| `audit/row.py` | 492 | Build and write the one audit row a finished document gets (spec §4.1, §4.2, §5.3; roadmap M7). | REPLACED BY RECORD | record events + version chain | graph/build_graph, pipeline/limits | structlog |
| `audit/sinks/__init__.py` | 222 | Trace sinks (spec §5.5): where a visit's prompt and completion, and pending rows, are kept. *trace sinks become optional exporters* | PORT AND DE-HARDCODE | `mailroom_engine/exporters/__init__.py` (optional, fed from events) | audit/sinks/langfuse, audit/sinks/otlp | structlog |
| `audit/sinks/braintrust.py` | 43 | The ``braintrust`` trace sink (opt-in, ``MAILROOM_TRACE_SINKS``): I/O logged on the current span. Wr… *not named in the design (Langfuse, OpenTelemetry are)* | DROP | - | - | braintrust |
| `audit/sinks/langfuse.py` | 65 | The ``langfuse`` trace sink (opt-in, ``MAILROOM_TRACE_SINKS``): I/O as a Langfuse event. Write-only. *only if The-Mailroom must read spans (design open question 2)* | PORT AND DE-HARDCODE | `mailroom_engine/exporters/langfuse.py` (extra) | observability/langfuse_setup | - |
| `audit/sinks/otlp.py` | 52 | The ``otlp`` trace sink (opt-in, ``MAILROOM_TRACE_SINKS``): I/O as span attributes on the global Ope… | PORT AND DE-HARDCODE | `mailroom_engine/exporters/otlp.py` (extra) | - | opentelemetry |
| `audit/sinks/phoenix.py` | 125 | The ``phoenix`` trace sink (spec §5.5): opt-in through ``MAILROOM_TRACE_SINKS`` (``sql,phoenix``). *covered by OTLP exporter* | DROP | - | - | opentelemetry, structlog |
| `audit/sinks/sql.py` | 222 | The ``sql`` trace sink (spec §5.5): always on. | REPLACED BY RECORD | - | - | sqlalchemy, structlog |
| `audit/trace.py` | 363 | Node visit records collected in ``state["audit_trace"]`` (spec §4.4, §5.2). | REPLACED BY RECORD | record events (node start/finish, status) | pipeline/config, pipeline/limits | - |
| `audit/verify.py` | 873 | Storage-scope verification of the unified audit storage (spec §6, roadmap M4). | REPLACED BY RECORD | `mailroom_record.verify` | - | sqlalchemy, structlog |
| `audit/writer.py` | 889 | Write the unified audit row, its sub-rows and its addenda (spec §4, §5.3). | REPLACED BY RECORD | `mailroom_record.log` | - | sqlalchemy, structlog |

#### `src/dojo_source` (1 modules, 174 lines)

| Module | Lines | What it does | Verdict | Target | Depends on (to port first) | Third-party |
|---|---:|---|---|---|---|---|
| `dojo_source/__init__.py` | 174 | Import ``llm_dojo_scoring`` from the installed release, with the vendored snapshot as backup. *no pins, one workspace* | DROP | - | - | - |

#### `src/graph` (10 modules, 8,864 lines)

| Module | Lines | What it does | Verdict | Target | Depends on (to port first) | Third-party |
|---|---:|---|---|---|---|---|
| `graph/__init__.py` | 3 |  | DROP | - | - | - |
| `graph/attempts.py` | 196 | Extraction attempt history and the opt-in "keep the best attempt" selection (#254). *attempt history; each attempt a blob in the version chain* | PORT AS IS | `mailroom_engine/attempts.py` | - | - |
| `graph/build_graph.py` | 5215 |  *5,215 lines; node bodies -> agents, wiring -> policy file, scoring nodes removed* | PORT AND DE-HARDCODE | `mailroom_engine/graph/compile.py` (policy compiler) + `config/policies/legacy.*` | agents/arbiter, agents/archivist, agents/boss, agents/contracts_specialist, agents/corporate_records_specialist, agents/correspondence_specialist (+37 more) | docx, langgraph, llm_dojo_scoring, structlog |
| `graph/contract.py` | 75 | The compiled pipeline graph, as the display contract describes it. *display contract for The-Mailroom; `engine graph --policy` draws from the policy file* | DROP | - | graph/build_graph, observability/tracing | langgraph |
| `graph/decision_points.py` | 1243 | Pluggable decision points: a decision model that refines a node's route. *8 slots of #309, caps per slot* | PORT AND DE-HARDCODE | `mailroom_engine/decisions/slots.py` + slot settings in the policy | agents/extraction_gate, graph/route_spec, llm/retry, observability/tracing, pipeline/config, pipeline/limits | langgraph, structlog |
| `graph/route_spec.py` | 553 | Graph routing as data. *closest existing thing to the policy format* | PORT AND DE-HARDCODE | `mailroom_engine/graph/policy.py` (routing as data -> policy file schema) | graph/routing | langgraph, structlog |
| `graph/routing.py` | 940 |  *940 lines of router functions; router replay gate runs these* | PORT AND DE-HARDCODE | `mailroom_engine/graph/conditions.py` + thresholds in `config/policies/legacy.*` | pipeline/config, pipeline/reconsideration | structlog |
| `graph/run_events.py` | 268 | Opt-in pipeline run journals for exact node ingress and outcome tracking. | REPLACED BY RECORD | record events | - | langchain_core |
| `graph/scoring_nodes.py` | 182 | Scoring node family: ``compile_report -> score_common -> score_<type> -> catalog_write`` (spec §3.1,… *engine scores nothing; the Dojo scores from the record* | DROP | - | observability/extraction_gt, schemas/documents | llm_dojo_scoring, structlog |
| `graph/state.py` | 189 |  | PORT AND DE-HARDCODE | `mailroom_engine/handoffs.py` (typed envelope) + state | - | langgraph |

#### `src/langchain_agents` (16 modules, 7,502 lines)

| Module | Lines | What it does | Verdict | Target | Depends on (to port first) | Third-party |
|---|---:|---|---|---|---|---|
| `langchain_agents/__init__.py` | 3 | Vendored LangChain agents from llm-entity-extraction. | DROP | - | - | - |
| `langchain_agents/base_agent.py` | 649 | Base agent class — LangChain-powered LLM helpers with structured output. *LangChain-OpenAI wrapper; provider-neutral client* | PORT AND DE-HARDCODE | merge into `mailroom_engine/agents/base.py` and `mailroom_engine/model_client.py` | langchain_agents/memory, langchain_agents/skills, langchain_agents/toolkit, llm/client, llm/retry, llm/vision (+2 more) | langchain_core, langchain_openai, openai, structlog |
| `langchain_agents/classifier.py` | 198 | Document classification via OpenRouter vision models. *vision classifier via OpenRouter `requests`; check if any live path uses it* | OPEN (not in design) | - | langchain_agents/openrouter_utils, pipeline/config | requests, structlog |
| `langchain_agents/cuad_maud.py` | 373 | CUAD families + 41 clause categories and MAUD consideration / clause labels. | BECOMES CONFIG | `config/clients/mailroom-dataset/schemas/` (same files as Dojo D3) | langchain_agents/sorter_agent | llm_dojo_scoring |
| `langchain_agents/doc_inventories.py` | 593 | Hub subclass inventories for every mailroom specialist. | BECOMES CONFIG | `config/clients/mailroom-dataset/` subclass inventories | langchain_agents/cuad_maud, langchain_agents/sorter_agent | llm_dojo_scoring |
| `langchain_agents/env_utils.py` | 66 | Environment variable helpers. *`mailroom_common.config` env handling* | DROP | - | - | dotenv |
| `langchain_agents/memory.py` | 138 | Per-agent OUTCOME MEMORY for the vendored LangChain agents. *per-agent outcome ledger under `data/memory/` fed back into retry prompts, no enable flag found: mutable state outside the record; a replay-parity risk for `legacy`* | OPEN (not in design) | - | - | structlog |
| `langchain_agents/mock.py` | 110 | MAILROOM-LOCAL (not from upstream): deterministic fake LangChain LLM. *deterministic fake LLM for mock e2e gate* | PORT AS IS | `mailroom_engine/testing/fake_model.py` | - | - |
| `langchain_agents/openrouter_utils.py` | 107 | Shared OpenRouter constants and request helpers. | PORT AND DE-HARDCODE | `mailroom_launcher/backends/openrouter.py` | - | - |
| `langchain_agents/prompt_candidates/__init__.py` | 1 | Local, explicitly selected offline prompt candidates; no production defaults. | BECOMES CONFIG | `config/prompts/<set>/` overlay | - | - |
| `langchain_agents/prompt_candidates/corporate_flat9.py` | 126 | DMR-091: literal Flat9 candidates, offline and unmeasured, never live defaults. | BECOMES CONFIG | `config/prompts/overlays/corporate_flat9/` (offline candidate) | - | - |
| `langchain_agents/prompts.py` | 3198 | All mailroom agent system prompts — versioned for iterative evaluation. *3,198 lines of prompt text* | BECOMES CONFIG | `config/prompts/deploy_v1/*.txt` (+ frozen lineage) | langchain_agents/prompt_candidates/corporate_flat9, llm/prompt_doctrine | - |
| `langchain_agents/skills.py` | 78 | Per-agent SKILL FILES for the vendored LangChain agents. *must be inside the hashed prompt set or the legacy match breaks* | BECOMES CONFIG | `config/prompts/deploy_v1/skills/` (28 md files, 650 lines, appended to system prompts today) | - | - |
| `langchain_agents/sorter_agent.py` | 559 | SorterAgent — Legal Document Classification Agent (LangChain). | PORT AND DE-HARDCODE | merge into `mailroom_engine/agents/sorter.py` | langchain_agents/base_agent, langchain_agents/classifier, langchain_agents/doc_inventories, langchain_agents/openrouter_utils, langchain_agents/prompts, pipeline/config | llm_dojo_scoring, structlog |
| `langchain_agents/specialist_agents.py` | 1136 | Specialist agents for field extraction from each document type (LangChain). *1,136 lines* | PORT AND DE-HARDCODE | merge into `mailroom_engine/agents/specialist.py` | langchain_agents/base_agent, langchain_agents/cuad_maud, langchain_agents/doc_inventories, langchain_agents/prompts, llm/retry, pipeline/config | structlog |
| `langchain_agents/toolkit.py` | 167 | Per-agent TOOL registry for the vendored LangChain agents. *read-only tool registry for agents* | OPEN (not in design) | - | langchain_agents/memory, langchain_agents/sorter_agent, pipeline/config, schemas/documents | llm_dojo_scoring, structlog |

#### `src/legalbench` (11 modules, 1,463 lines)

| Module | Lines | What it does | Verdict | Target | Depends on (to port first) | Third-party |
|---|---:|---|---|---|---|---|
| `legalbench/__init__.py` | 16 | LegalBench evaluation suite — a second lens on model quality. *LegalBench dropped by the design* | DROP | - | - | - |
| `legalbench/agent.py` | 93 | Model agent for LegalBench runs. *LegalBench dropped by the design* | DROP | - | langchain_agents/base_agent | - |
| `legalbench/cli.py` | 82 | LegalBench suite CLI. *LegalBench dropped by the design* | DROP | - | - | - |
| `legalbench/data.py` | 230 | Local corpus loaders for the LegalBench suite. *LegalBench dropped by the design* | DROP | - | - | - |
| `legalbench/experiment_log.py` | 300 | Experiment-log integration for LegalBench runs. *LegalBench dropped by the design* | DROP | - | - | - |
| `legalbench/langfuse_tracing.py` | 132 | Langfuse tracing for LegalBench runs. *LegalBench dropped by the design* | DROP | - | - | - |
| `legalbench/mock.py` | 68 | Deterministic mock model for LegalBench runs (no network, no OpenAI). *LegalBench dropped by the design* | DROP | - | - | - |
| `legalbench/prompts.py` | 78 | Versioned LegalBench task prompts. *LegalBench dropped by the design* | DROP | - | langchain_agents/sorter_agent | - |
| `legalbench/runner.py` | 309 | LegalBench run orchestrator. *LegalBench dropped by the design* | DROP | - | observability/tracing | llm_dojo_scoring |
| `legalbench/scoring.py` | 16 | LegalBench compatibility facade. Requires the new reviewed Dojo release. *LegalBench dropped by the design* | DROP | - | langchain_agents/sorter_agent | llm_dojo_scoring |
| `legalbench/tasks.py` | 139 | LegalBench task registry. *LegalBench dropped by the design* | DROP | - | langchain_agents/sorter_agent | - |

#### `src/llm` (12 modules, 2,316 lines)

| Module | Lines | What it does | Verdict | Target | Depends on (to port first) | Third-party |
|---|---:|---|---|---|---|---|
| `llm/__init__.py` | 3 |  | DROP | - | - | - |
| `llm/client.py` | 284 |  *no agent names a provider* | PORT AND DE-HARDCODE | `mailroom_engine/model_client.py` (`get_llm` -> profile map); interface in `mailroom_common.models` | llm/providers, observability/tracing, pipeline/config | openai |
| `llm/frozen_v1/__init__.py` | 136 | Frozen v1 specialist prompts for the LangGraph extract nodes. | BECOMES CONFIG | `config/prompts/frozen_v1/` | - | llm_dojo_scoring |
| `llm/prompt_doctrine.py` | 269 | Production pipeline doctrine appended onto frozen predecessor prompts. | BECOMES CONFIG | `config/prompts/deploy_v1/` doctrine overlay text | - | - |
| `llm/prompt_sets/__init__.py` | 151 | Versioned prompt sets: which system prompt each agent serves (``catalog.yaml``). | PORT AND DE-HARDCODE | `mailroom_engine/prompts.py` loader; `catalog.yaml` -> `config/prompts/catalog.yaml` | llm/prompts, pipeline/config | structlog, yaml |
| `llm/prompts.py` | 257 | Agent prompts: the repo prompt by default, a Langfuse-managed override only on opt-in. *records prompt id and hash* | PORT AND DE-HARDCODE | `mailroom_engine/prompts.py` (repo prompt sets only; drop Langfuse override) | langchain_agents/prompts, observability/langfuse_setup, observability/tracing, pipeline/config | structlog |
| `llm/provider_health.py` | 147 | Passive per-provider LLM health for the operator pager. *breaker per provider (failure table)* | PORT AND DE-HARDCODE | `mailroom_engine/breakers.py` | llm/retry | - |
| `llm/providers.py` | 202 |  | PORT AND DE-HARDCODE | `mailroom_launcher/backends/` + client profile | - | - |
| `llm/quota.py` | 93 | Free-model quota circuit breaker. *free-model quota breaker* | PORT AND DE-HARDCODE | `mailroom_engine/breakers.py` | pipeline/config | - |
| `llm/result_cache.py` | 99 | On-disk cache of validated LLM results, keyed by model + prompt + input. *each model response is in the record* | REPLACED BY RECORD | replay client / resume skip | - | structlog |
| `llm/retry.py` | 430 | Transient-failure retry for LLM chat completions. | PORT AND DE-HARDCODE | `mailroom_engine/model_client.py` on `mailroom_common.retry` | llm/client, llm/provider_health, llm/quota, pipeline/config, pipeline/limits | openai, structlog |
| `llm/vision.py` | 245 | Vision-capable model helpers for the mailroom pipeline. | PORT AS IS | `mailroom_engine/vision.py` | llm/client, pipeline/config | fitz, structlog |

#### `src/observability` (20 modules, 5,032 lines)

| Module | Lines | What it does | Verdict | Target | Depends on (to port first) | Third-party |
|---|---:|---|---|---|---|---|
| `observability/__init__.py` | 3 |  | DROP | - | - | - |
| `observability/agent_eval.py` | 434 | Per-agent isolation eval — invoke one mailroom agent against labeled cases. | PORT AND DE-HARDCODE | `mailroom_launcher/agent_eval.py` (runs one agent) + Dojo suites | agents/arbiter, agents/boss, agents/image_extractor, agents/judge, agents/pdf_transcriber, agents/reporter (+3 more) | llm_dojo_scoring |
| `observability/bootstrap.py` | 14 | Compatibility imports for the Dojo-owned bootstrap engine. *Dojo facade* | DROP | - | - | llm_dojo_scoring |
| `observability/braintrust_setup.py` | 278 | Braintrust tracing backend — alternative to Langfuse. | DROP | - | - | braintrust, structlog |
| `observability/classification_scoring.py` | 8 | Pipeline exact KPI compatibility facade; requires reviewed Dojo API release. *Dojo facade* | DROP | - | - | llm_dojo_scoring |
| `observability/extraction_gt.py` | 399 | Build scorable extraction GT for every live specialist class. *post-hoc GT is not a label layer in the design (R6)* | OPEN (not in design) | labels package (provenance-tagged post-hoc fills) or drop | langchain_agents/cuad_maud, langchain_agents/doc_inventories, langchain_agents/sorter_agent, observability/posthoc_gt | - |
| `observability/honest_gaps.py` | 135 | Surface llm-dojo-scoring honesty gaps without inventing metrics. *scorecard honesty replaced by number provenance* | DROP | - | - | llm_dojo_scoring |
| `observability/interim_gt.py` | 257 | Staged ground-truth overlay: interim labels the published Hub merge does not carry yet. *replaced by `labels/interim-2026-11-01/` snapshot* | DROP | - | langchain_agents/doc_inventories | - |
| `observability/langfuse_field_scoring.py` | 286 | Wire deterministic field scoring into Langfuse (GitHub issue #5). *Dojo scores from the record* | DROP | - | - | llm_dojo_scoring, structlog |
| `observability/langfuse_setup.py` | 370 | Langfuse tracing backend (langfuse >= 4.x). *only as exporter* | PORT AND DE-HARDCODE | `mailroom_engine/exporters/langfuse.py` | observability/masking | langfuse, opentelemetry, structlog |
| `observability/local_eval_packs.py` | 343 | Local eval packs that close honesty gaps Hub cannot. | DROP | - | - | llm_dojo_scoring |
| `observability/masking.py` | 70 | Export-stage PII masking for Langfuse spans (langfuse >= 4.6 ``mask_otel_spans``). *logs carry our tokens only* | PORT AND DE-HARDCODE | `mailroom_engine/exporters/masking.py` | - | langfuse |
| `observability/metrics.py` | 43 | Adapt pipeline diagnostic rows to the Dojo-owned metric engine. *Dojo adapter* | DROP | - | - | llm_dojo_scoring |
| `observability/phoenix_setup.py` | 132 | Arize Phoenix tracing backend — local, cost-free default for llm-mailroom. | DROP | - | - | openinference, opentelemetry |
| `observability/posthoc_gt.py` | 592 | Deterministic post-hoc extraction GT from document text. | OPEN (not in design) | see extraction_gt | langchain_agents/cuad_maud | - |
| `observability/scores.py` | 677 | Quality scores for document runs. *quality scores now Dojo reports* | DROP | - | observability/langfuse_setup, observability/tracing, pipeline/config, pipeline/limits, schemas/documents | langfuse, llm_dojo_scoring, structlog |
| `observability/scoring_wiring.py` | 47 | Mailroom taxonomy -> llm-dojo-scoring wiring (single thin caller). | DROP | - | pipeline/config | llm_dojo_scoring, structlog |
| `observability/specialist_suites.py` | 179 | Dedicated scoring suite for every live mailroom specialist. | DROP | - | pipeline/config, schemas/documents | llm_dojo_scoring |
| `observability/suite_scoring.py` | 243 | Dedicated specialist scoring suites from the pinned llm-dojo-scoring release. | DROP | - | - | llm_dojo_scoring, structlog |
| `observability/tracing.py` | 522 | Observability facade — picks the active tracing backend. *engine works with no trace sink* | PORT AND DE-HARDCODE | `mailroom_engine/exporters/` facade (no-op default) | observability/langfuse_setup | langgraph, structlog |

#### `src/pipeline` (34 modules, 17,001 lines)

| Module | Lines | What it does | Verdict | Target | Depends on (to port first) | Third-party |
|---|---:|---|---|---|---|---|
| `pipeline/__init__.py` | 3 |  | DROP | - | - | - |
| `pipeline/bins.py` | 1023 |  *files move only through the record/queue; human-readable bins could be a launcher view* | REPLACED BY RECORD | record + `mailroom_engine/queue.py` states | pipeline/config, schemas/manifest | structlog |
| `pipeline/config.py` | 481 |  *481 lines* | PORT AND DE-HARDCODE | `mailroom_engine/config.py` (reads `config/clients`, policies; no lru_cache globals) | - | yaml |
| `pipeline/decision_review.py` | 108 | Pure routing for a decision model acting as the Lane A reviewer. *pure routing for Lane A slot* | PORT AND DE-HARDCODE | `mailroom_engine/decisions/sorter_review.py` | pipeline/reconsideration | - |
| `pipeline/env.py` | 38 | Load environment variables from a .env file. *`mailroom_common.config`* | DROP | - | - | dotenv |
| `pipeline/extraction_normalize.py` | 42 | Normalize specialist extractions before guardrails and routing. | PORT AS IS | `mailroom_engine/agents/normalize.py` | langchain_agents/specialist_agents, observability/posthoc_gt | - |
| `pipeline/failures.py` | 99 | Classify pipeline crashes so aborted runs are distinguishable. | PORT AND DE-HARDCODE | `mailroom_engine/failures.py` -> error event cause tokens | llm/retry, pipeline/limits | - |
| `pipeline/gmail_intake.py` | 2722 | Gmail intake channel — the mailroom agent mailbox as a second intake route. *2,722-line Gmail channel; not in the design* | OPEN (not in design) | - | pipeline/relations | structlog |
| `pipeline/guards.py` | 224 | Guardrails for agent outputs. *vocabularies from `mailroom_dictionaries`* | PORT AND DE-HARDCODE | `mailroom_engine/guards.py` | langchain_agents/doc_inventories, langchain_agents/sorter_agent, pipeline/config | structlog |
| `pipeline/hf_corpora.py` | 365 | Lucius-Morningstar Hugging Face corpora the mailroom pipeline can ingest. | PORT AND DE-HARDCODE | `mailroom_launcher/draws.py` (document draw) | - | - |
| `pipeline/hf_corpus_loader.py` | 698 | Canonical mailroom-dataset HuggingFace dataset loader (HUB-053). *pinned dataset revision; labels package owns label import* | PORT AND DE-HARDCODE | `mailroom_launcher/draws.py` | pipeline/hf_corpora | httpx, pandas |
| `pipeline/label_vocab.py` | 107 | Closed-label vocabularies and the out-of-vocabulary check (#285). *closed vocabularies live in `mailroom_dictionaries`* | DROP | - | pipeline/config | llm_dojo_scoring |
| `pipeline/limits.py` | 332 | Run-limit enforcement for document runs. *spend gate stays in launcher* | PORT AND DE-HARDCODE | `mailroom_engine/caps.py` (cap from the record running total) | pipeline/config | structlog |
| `pipeline/live_manifest.py` | 523 | Live ``mailroom-dataset`` manifest: resolve, verify, cache and emit CSV. | PORT AND DE-HARDCODE | `mailroom_launcher/draws.py` | langchain_agents/cuad_maud, langchain_agents/doc_inventories, langchain_agents/sorter_agent, pipeline/config, pipeline/hf_corpus_loader, schemas/documents (+1 more) | httpx, pandas |
| `pipeline/logging.py` | 113 | Structured logging setup for Mailroom entrypoints. *`mailroom_common.logging`* | DROP | - | - | structlog |
| `pipeline/mail_guards.py` | 166 | Gmail intake sender authentication and mail-loop guards. *Gmail* | OPEN (not in design) | - | - | - |
| `pipeline/mail_outbox.py` | 499 | Durable SQLite outbox for Gmail completion echoes (HUB-037). *Gmail* | OPEN (not in design) | - | pipeline/gmail_intake | structlog |
| `pipeline/ops_monitor.py` | 187 |  | OPEN (not in design) | `mailroom_launcher` ops or drop | agents/boss, observability/tracing | llm_dojo_scoring, structlog |
| `pipeline/pager.py` | 849 | Operator SMS pager host: snapshot sources, scheduler, settings, CLI. *SMS pager* | OPEN (not in design) | - | langchain_agents/openrouter_utils, llm/provider_health, llm/quota, pipeline/config, pipeline/pager_core, pipeline/sms_notify | httpx, structlog |
| `pipeline/pager_core.py` | 834 | Operator SMS pager core (shared verbatim with mailroom-reloaded/obs/pager_core.py). *shared with mailroom-reloaded* | OPEN (not in design) | - | - | - |
| `pipeline/policy_replay.py` | 1096 | Offline policy replay and the hill-climb objective. *reads `RunRecord`; must read the record* | PORT AND DE-HARDCODE | replay -> `mailroom_engine/replay.py`; objective -> Dojo keep rule | graph/routing, langchain_agents/specialist_agents, pipeline/config, pipeline/reconsideration | llm_dojo_scoring, yaml |
| `pipeline/reconsideration.py` | 687 | Objective review / reconsideration causes. *The-Mailroom mirrors it* | PORT AND DE-HARDCODE | `mailroom_engine/reconsideration.py`; causes -> `review_causes` dictionary | pipeline/config | - |
| `pipeline/relations.py` | 1036 | Relations — the mailroom's research clerk (HUB-040). *1,036 lines; storage via record* | PORT AND DE-HARDCODE | `mailroom_engine/relations/` | agents/relations, graph/build_graph, observability/tracing, pipeline/config | llm_dojo_scoring, sqlalchemy, structlog |
| `pipeline/relations_graph.py` | 471 | Relations knowledge graphs (HUB-040). *matplotlib/plotly graphs* | DROP | - | pipeline/relations | matplotlib, plotly, sqlalchemy, structlog |
| `pipeline/relations_mode.py` | 516 | Relations clerk mode toggle (HUB-052) — live vs pilot, simply. | BECOMES CONFIG | policy/config | llm/client, pipeline/config, pipeline/relations | structlog, yaml |
| `pipeline/relations_scan.py` | 15 | Relations scanner CLI (HUB-040/HUB-051) — the documented entrypoint. *CLI* | OPEN (not in design) | - | pipeline/relations | - |
| `pipeline/review_priority.py` | 139 | Priority of a parked document in the human review tray. *plugs into the review station* | REVIEW STATION (other agent) | review queue priority | pipeline/config | - |
| `pipeline/review_resolve.py` | 909 | Human-review resolve dispositions for the REVIEW tray / The-Mailroom proxy. *resolve dispositions; corrections become new versions* | REVIEW STATION (other agent) | review station | graph/build_graph, pipeline/config, schemas/documents, schemas/manifest | structlog |
| `pipeline/sms_notify.py` | 186 | Operator SMS pager transports: Twilio, carrier email gateway and console, with recipient validation,… | OPEN (not in design) | - | - | httpx, structlog |
| `pipeline/status_notify.py` | 175 | Watcher status notifications (HUB-050). | OPEN (not in design) | - | pipeline/gmail_intake | structlog |
| `pipeline/synthetic_records.py` | 370 | Seeded synthetic ``RunRecord`` generator with a PLANTED calibration curve. *planted calibration curve* | PORT AND DE-HARDCODE | Dojo test kit `packages/dojo/tests/synthetic_runs.py` (writes records) | pipeline/policy_replay | - |
| `pipeline/text_cleaning.py` | 78 | Conservative Markdown presentation cleanup for intake, without an LLM. | PORT AS IS | `mailroom_engine/agents/intake_cleaning.py` | - | markdown_it |
| `pipeline/watchdog.py` | 213 | External watcher watchdog (HUB-050): 🔴 down alerts + 🟠 reminders only. | OPEN (not in design) | - | pipeline/pager, pipeline/pager_core, pipeline/status_notify | structlog |
| `pipeline/watcher.py` | 1697 |  *1,697 lines; claims by content hash, leases* | PORT AND DE-HARDCODE | `mailroom_engine/workers.py` + `mailroom_engine/ingest/folder_watch.py` | agents/gmail_triage, agents/intake, graph/build_graph, graph/decision_points, observability/masking, observability/tracing (+8 more) | llm_dojo_scoring, pdfplumber, pypdf, structlog, watchdog |

#### `src/schemas` (6 modules, 720 lines)

| Module | Lines | What it does | Verdict | Target | Depends on (to port first) | Third-party |
|---|---:|---|---|---|---|---|
| `schemas/__init__.py` | 24 |  | DROP | - | schemas/documents, schemas/manifest, schemas/matter | - |
| `schemas/audit.py` | 133 |  | REPLACED BY RECORD | `mailroom_record.models` | - | pydantic |
| `schemas/documents.py` | 182 |  | PORT AND DE-HARDCODE | `mailroom_engine/models.py`; field lists -> schema config | pipeline/config | pydantic |
| `schemas/manifest.py` | 108 |  | PORT AND DE-HARDCODE | `mailroom_engine/models.py` | - | pydantic |
| `schemas/matter.py` | 10 |  | PORT AS IS | `mailroom_engine/models.py` | - | pydantic |
| `schemas/run_record.py` | 263 | Per-document run record: the contract between experiments and the hill climb. *per-document RunRecord used by hill climb* | REPLACED BY RECORD | `mailroom_record` events | - | pydantic |

#### `src/scripts` (51 modules, 22,140 lines)

| Module | Lines | What it does | Verdict | Target | Depends on (to port first) | Third-party |
|---|---:|---|---|---|---|---|
| `scripts/__init__.py` | 2 |  | DROP | - | - | - |
| `scripts/affected_tests.py` | 226 | Select the test files a change can affect, so a task runs only those. *workspace Makefile* | DROP | - | - | - |
| `scripts/analyze_audit_db.py` | 145 | Parse and analyze the full local audit DB (and optional catalog join). | REPLACED BY RECORD | record `trace` + Dojo | - | - |
| `scripts/audit_reconcile.py` | 99 | Write the audit rows that are waiting in the trace sinks (unified audit row spec §5.6 step 3). | REPLACED BY RECORD | - | - | structlog |
| `scripts/audit_report.py` | 60 | Write the audit report: one Markdown section per live document type (unified audit row spec §7). | REPLACED BY RECORD | Dojo report | - | structlog |
| `scripts/autoresearch_gate.py` | 295 | Keep-or-discard gate for one autoresearch hill-climb iteration. *per-class guard rewritten for noise* | PORT AND DE-HARDCODE | keep rule -> `mailroom_dojo/compare/keep_rule.py`; driver -> launcher | - | yaml |
| `scripts/bench_throughput.py` | 883 | Offline load benchmark for the document graph (Phase 0: measure before changing anything). *throughput floor gate (249/min)* | PORT AS IS | `packages/engine/scripts/bench_throughput.py` | graph/build_graph, pipeline/config, scripts/stress_test | httpx, openai |
| `scripts/build_legacy_audit_fixture.py` | 223 | Build ``src/tests/fixtures/audit/legacy_audit.db`` (roadmap M2). | REPLACED BY RECORD | - | - | sqlalchemy |
| `scripts/build_mascot.py` | 477 | Build Fumi, the llm-mailroom mascot. | DROP | - | - | PIL |
| `scripts/bump_dojo_scoring.py` | 718 | Bump (or check) the llm-dojo-scoring git pin across the repo. *no pins* | DROP | - | - | - |
| `scripts/calibrate_field_scoring.py` | 347 | Calibrate the deterministic field-scoring thresholds (issue #4). *field thresholds* | PORT AND DE-HARDCODE | `packages/dojo/scripts/` | - | llm_dojo_scoring |
| `scripts/calibrate_thresholds.py` | 645 | Propose per-class confidence cutoffs from ``RunRecord`` JSONL, with an evidence gate. | PORT AND DE-HARDCODE | Dojo confidence report + threshold refit | - | llm_dojo_scoring, yaml |
| `scripts/ci_tests.py` | 455 | Plan and run a tiered, conservative test cascade; unknown impact means a full run. *workspace Makefile* | DROP | - | - | - |
| `scripts/compare_runs.py` | 147 | Compare document runs against each other using catalog metrics. | PORT AND DE-HARDCODE | Dojo comparison (paired) | - | - |
| `scripts/cutover.py` | 185 | Phase 10: Agent-by-agent local model cutover utility. | PORT AND DE-HARDCODE | `mailroom_launcher` model-map validation | - | yaml |
| `scripts/export_contract.py` | 163 | Export the pipeline's display contract for The-Mailroom. *replaced by `engine graph --policy`* | DROP | - | langchain_agents/doc_inventories, observability/tracing | yaml |
| `scripts/export_review_labels.py` | 431 | Export human review resolutions as calibration labels. | REVIEW STATION (other agent) | labels candidates from corrections | pipeline/reconsideration | - |
| `scripts/export_warehouse.py` | 111 | Export finished catalog documents + audit chains to Parquet warehouse files. | REPLACED BY RECORD | - | - | - |
| `scripts/fetch_external_samples.py` | 255 | Fetch the external pilot samples from LegalBench, The Atticus Project, and Pile of Law. | DROP | - | - | - |
| `scripts/fetch_full_cuad.py` | 427 | Download the FULL CUAD contract dataset for processing, analysis, and EDA. | DROP | - | langchain_agents/sorter_agent | huggingface_hub |
| `scripts/gmail_smoke_test.py` | 730 | Gmail + watcher connectivity smoke test (HUB-037). *Gmail* | OPEN (not in design) | - | langchain_agents/base_agent, langchain_agents/mock, pipeline/watcher | - |
| `scripts/hillclimb_eval.py` | 198 | Score a confidence-threshold policy on run records: the hill climb's objective. | PORT AND DE-HARDCODE | Dojo keep rule objective | - | - |
| `scripts/new_report.py` | 90 | Scaffold a new evaluation write-up / audit / report under docs/reports/. | DROP | - | - | - |
| `scripts/prepare_samples.py` | 141 | Build the pilot sample PDF set, driven by examples/samples/manifest.csv. | PORT AND DE-HARDCODE | `mailroom_launcher` fixtures/draws | - | reportlab |
| `scripts/probe_hosted_spaces.py` | 172 | Probe the live Hugging Face Observatory + producer pair. *HF Space* | OPEN (not in design) | - | - | - |
| `scripts/publish_space.py` | 335 | Publish the llm-mailroom producer API to a Hugging Face Docker Space. *HF Space* | OPEN (not in design) | - | - | huggingface_hub |
| `scripts/pull_live_manifest.py` | 151 | Pull the live mailroom-dataset manifest into the pipeline sample shape. | PORT AND DE-HARDCODE | `mailroom_launcher/draws.py` | pipeline/live_manifest | - |
| `scripts/recover_processing.py` | 158 | Reconcile processing/ claims orphaned by crashed processes (A-18/L-1). | REPLACED BY RECORD | leases + resume from record | - | - |
| `scripts/release.py` | 752 | Release governor for llm-mailroom: status, check, cut, notes, publish. *workspace release later* | DROP | - | - | - |
| `scripts/roadmap_status.py` | 286 | Status, alignment check and issue body for the unified audit row roadmap. | DROP | - | - | - |
| `scripts/run_agent_eval.py` | 139 | Evaluate one mailroom LLM agent in isolation (no 14-node graph). | PORT AND DE-HARDCODE | `mailroom_launcher` CLI | agents/base, langchain_agents/base_agent, langchain_agents/mock, llm/client, observability/agent_eval | - |
| `scripts/run_calibration_sweep.py` | 1554 | Full-information calibration sweep: one ``RunRecord`` JSON line per document. | PORT AND DE-HARDCODE | `mailroom_launcher` run spec + Dojo | agents/sorter, agents/sorter_reviewer, agents/source_windows, graph/build_graph, langchain_agents/base_agent, langchain_agents/doc_inventories (+4 more) | llm_dojo_scoring, pandas |
| `scripts/run_experiment.py` | 415 | Run a Langfuse-style dataset experiment over the pilot manifest and gate it. *mock fixture gate source* | PORT AND DE-HARDCODE | `mailroom_launcher` experiment run + Dojo gate | llm/prompts, pipeline/config, pipeline/pager, scripts/prepare_samples | langfuse, llm_dojo_scoring |
| `scripts/run_hf_pilot.py` | 2002 | Hugging Face corpus pilot — the runner The-Mailroom orchestrates. | PORT AND DE-HARDCODE | `mailroom_launcher` run CLI | agents/intake, graph/build_graph, langchain_agents/base_agent, langchain_agents/cuad_maud, langchain_agents/doc_inventories, langchain_agents/sorter_agent (+5 more) | datasets, httpx, llm_dojo_scoring |
| `scripts/run_pilot.py` | 1205 | Pilot-test the pipeline against examples/samples/manifest.csv or the pinned HF corpus. *mock e2e gate source* | PORT AND DE-HARDCODE | `mailroom_launcher` run CLI | graph/build_graph, langchain_agents/base_agent, langchain_agents/mock, llm/client, llm/prompts, observability/langfuse_setup (+6 more) | httpx, llm_dojo_scoring, structlog |
| `scripts/run_quality_judges.py` | 457 | Run the LLM-as-a-judge evaluators over a pilot run. *LLM judges over a run* | OPEN (not in design) | - | agents/judge, agents/pdf_transcriber, observability/langfuse_setup, observability/tracing, scripts/prepare_samples, scripts/run_hf_pilot | structlog |
| `scripts/run_vision_sweep.py` | 153 | Vision tradeoff sweep: run the same real documents under different vision configurations and record … | PORT AND DE-HARDCODE | `mailroom_launcher` run spec | - | structlog |
| `scripts/smoke_live_documents.py` | 436 | Run real mailroom-dataset documents through the pipeline and record flow. | PORT AND DE-HARDCODE | `mailroom_launcher` smoke | graph/build_graph, langchain_agents/base_agent, langchain_agents/mock, pipeline/live_manifest, scripts/run_pilot | - |
| `scripts/smoke_modal_tiers.py` | 659 | End-to-end smoke: one document per class through the full stack, then prove which Modal GPU tier served… | PORT AND DE-HARDCODE | `mailroom_launcher/backends/modal` smoke | graph/build_graph, llm/client, observability/tracing, pipeline/config | httpx, langfuse, openai, yaml |
| `scripts/stress_test.py` | 1615 | Scale hill-climb evaluator: hermetic volume + fault-injection harness. | PORT AS IS | `packages/engine/tests/` fault harness | graph/build_graph, langchain_agents/base_agent, langchain_agents/doc_inventories, langchain_agents/mock | dotenv, httpx, llm_dojo_scoring, openai |
| `scripts/sync_dashboards.py` | 628 | Sync the mailroom health dashboards into Langfuse (idempotent). *hosted write* | DROP | - | observability/langfuse_setup | structlog |
| `scripts/sync_dataset.py` | 339 | Build the Mailroom evaluation datasets in the connected Langfuse project. *hosted write* | DROP | - | agents/pdf_transcriber, observability/langfuse_setup, pipeline/hf_corpus_loader, schemas/documents, scripts/prepare_samples | langfuse, structlog |
| `scripts/sync_evaluators.py` | 559 | Configure the LLM-as-a-Judge evaluators in the connected Langfuse project. *hosted write* | DROP | - | observability/langfuse_setup, pipeline/config | httpx, langfuse, structlog |
| `scripts/sync_gitbook_changelog.py` | 371 | Mirror repository CHANGELOG.md into the GitBook Changelog space. | DROP | - | - | - |
| `scripts/sync_hf_ground_truth.py` | 729 | Derive + publish the intent/subject_matter/keywords ground truth for ``Lucius-Morningstar/mailroom-d… *labels package owns GT* | DROP | - | langchain_agents/doc_inventories, llm/client, llm/retry, pipeline/hf_corpora | datasets, httpx, huggingface_hub, pyarrow, structlog |
| `scripts/sync_langfuse_logs.py` | 230 | Mirror Langfuse run logs into the local repository for offline analysis. *nothing reads traces back* | DROP | - | observability/langfuse_setup | structlog |
| `scripts/sync_models.py` | 179 | Sync the mailroom cost model prices into the Langfuse model registry. | DROP | - | observability/langfuse_setup, pipeline/config | langfuse, structlog |
| `scripts/sync_prompts.py` | 112 | Push the local agent prompts into Langfuse Prompt Management. *prompts never come from a trace service* | DROP | - | llm/prompts, observability/langfuse_setup | structlog |
| `scripts/validate_pipeline.py` | 528 | Validate the pipeline against the repository's own documents. | PORT AND DE-HARDCODE | `mailroom_launcher` mock validation | graph/build_graph, langchain_agents/base_agent, langchain_agents/mock | - |
| `scripts/verify_audit_chains.py` | 200 | Verify the hash-chained audit storage (audit A-8; roadmap M4). | REPLACED BY RECORD | `mailroom_record verify` | - | - |
| `scripts/write_pilot_report.py` | 323 | Write the vision-tradeoff pilot report as tracked markdown with JSON blocks. | PORT AND DE-HARDCODE | Dojo report | - | - |

#### `src/storage` (8 modules, 3,921 lines)

| Module | Lines | What it does | Verdict | Target | Depends on (to port first) | Third-party |
|---|---:|---|---|---|---|---|
| `storage/__init__.py` | 1 |  | REPLACED BY RECORD | - | - | - |
| `storage/audit_log.py` | 521 |  | REPLACED BY RECORD | `mailroom_record.log` | - | sqlalchemy, structlog |
| `storage/audit_migrate.py` | 729 | One-time migration of the per-event ``audit_log`` to the unified audit row. *0.30 audit DB import is after cutover* | REPLACED BY RECORD | - | - | sqlalchemy, structlog |
| `storage/catalog.py` | 490 |  | REPLACED BY RECORD | `mailroom_record.index` | pipeline/gmail_intake | sqlalchemy, structlog |
| `storage/db.py` | 484 |  | REPLACED BY RECORD | `mailroom_record.index` (SQLite/Postgres) | - | sqlalchemy, structlog |
| `storage/loop_bridge.py` | 287 | One long-lived event loop per process for sync callers of the async storage. | DROP | - | - | structlog |
| `storage/relations.py` | 610 | Relations storage — the auditable relations ledger (HUB-040). *relations ledger* | REPLACED BY RECORD | record events + blobs | - | sqlalchemy |
| `storage/warehouse.py` | 799 | Parquet cold store for finished documents + matching audit chains. | REPLACED BY RECORD | sealed column files | - | pyarrow, sqlalchemy, structlog |

#### llm-mailroom non-module assets

| Asset | Files | Lines | Verdict and target |
|---|---:|---:|---|
| `src/config/taxonomy.yaml` | 1 | 885 | BECOMES CONFIG, split by section (table below) |
| `src/llm/prompt_sets/` (`catalog.yaml`, `deploy_v1/*.txt`, `deploy_v1_examples/`) | 8 | 367 | BECOMES CONFIG: `config/prompts/deploy_v1/`, `config/prompts/catalog.yaml`. Only 6 agents have a `deploy_v1` file; the rest of the effective prompts are in `langchain_agents/prompts.py` (3,198 lines) and `llm/prompt_doctrine.py`, so the set must be materialised from code to text before hashing |
| `src/llm/frozen_v1/` (txt + `lineage.json`) | 6 | 291 | BECOMES CONFIG: `config/prompts/frozen_v1/` |
| `src/langchain_agents/skills/` (md) | 28 | 650 | BECOMES CONFIG inside the hashed prompt set (appended to system prompts today) |
| `deploy/` (compose files, `modal_vllm.py`, LiteLLM, llamafile, HF Space) | 16 | 1693 | launcher backends (L2); `modal_vllm.py` is 595 lines; the HF Space payload is OPEN |
| `Dockerfile`, `src/config/docker/` | 1 + 2 | 116 + 124 | launcher Compose backend (L2) |
| `tools/pipeline-diagnostics/` (Python, no tests / front-end) | 13 / 27 | 4,349 / 4,477 | DROP: inspect/check over old run folders and bins; `mailroom_record` trace/verify replace it (OPEN if the owner wants its workbench UI) |
| `tools/gt-label-workbook/` | 1 | 1,551 | labels package tooling (labels thread decides) |

#### `taxonomy.yaml` sections (start line, length)

| Section | Line | Lines | Goes to |
|---|---:|---:|---|
| `pipeline` | 5 | 14 | `config/policies/legacy.*` (pipeline switches) or run spec |
| `llm_retry` | 19 | 16 | policy retry budgets / engine client config |
| `vllm_model_map` | 35 | 10 | launcher backend config (`config/backends/`, new folder) + client profile model map |
| `ollama_model_map` | 45 | 10 | as vllm_model_map |
| `llamafile_model_map` | 55 | 19 | as vllm_model_map |
| `gateway` | 74 | 30 | launcher backend config (LiteLLM gateway) |
| `free_model_swarm` | 104 | 8 | OPEN: free-model fallback; provider-neutral client profile |
| `free_quota` | 112 | 4 | engine breaker config |
| `pager` | 116 | 51 | OPEN (pager not in design) |
| `watcher` | 167 | 3 | engine worker / ingest config |
| `run_limits` | 170 | 20 | run spec caps (launcher) read by engine caps |
| `chunking` | 190 | 27 | engine window config |
| `cost_models` | 217 | 32 | price sheet (launcher preflight + Dojo cost truth) |
| `confidence` | 249 | 61 | each class's sorter thresholds (`by_class`) -> the client profile (`config/clients/<client>/`, spec line 58); run-level routing thresholds -> `config/policies/legacy.*` (spec line 400) |
| `routing` | 310 | 14 | `config/policies/legacy.*` |
| `prompt_source` | 324 | 8 | DROP (always repo prompts) |
| `prompt_set` | 332 | 14 | run spec (`prompt_set` + overlays) |
| `reviewer` | 346 | 43 | `config/policies/legacy.*` decision slot "sorter reviewer" |
| `decision_points` | 389 | 2 | `config/policies/legacy.*` decision slots (empty `{{}}` today) |
| `review_queue` | 391 | 50 | review station config (other agent) + policy caps |
| `field_scoring` | 441 | 42 | PORTED (`config/clients/mailroom-dataset/scoring.yaml`) |
| `vision` | 483 | 27 | client profile / engine vision config |
| `doc_classes` | 510 | 140 | PORTED (`config/clients/mailroom-dataset/profile.yaml`, labels) |
| `file_extensions` | 650 | 28 | engine ingest config |
| `relations` | 678 | 24 | policy (relations node on/off, thresholds) |
| `audit` | 702 | 3 | REPLACED BY RECORD |
| `agents` | 705 | 181 | client profile agent-to-model map (run spec); no model names in policies |

### 1c. The-Mailroom

All of these read Langfuse (or Phoenix) traces today; README badge: "data source: Langfuse only". In the new layout they
read the record (R7: "reading the run record instead of sandbox run folders"; the replay timeline comes from the record package).


#### `mailroom_ui` (22 modules, 6,365 lines)

| Module | Lines | What it does | Verdict | Target | Depends on (to port first) | Third-party |
|---|---:|---|---|---|---|---|
| `mailroom_ui/__init__.py` | 34 | The-Mailroom — pixel-art visualization console for the llm-mailroom pipeline. | DROP | - | - | - |
| `mailroom_ui/classification.py` | 239 | Primary / secondary classification outcomes and archive filenames. *primary/secondary outcome display from record outcomes* | PORT AND DE-HARDCODE | `mailroom_launcher/floor/outcomes.py` | mailroom_ui/pipeline_schema | - |
| `mailroom_ui/docclass_prompts.py` | 120 | Vendored mirror of llm-entity-extraction's docclass prompt family. *vendored prompt mirror* | DROP | - | - | - |
| `mailroom_ui/env.py` | 41 | Tolerant numeric env knobs. | DROP | - | - | - |
| `mailroom_ui/hf_corpus.py` | 158 | Hugging Face corpus pin for The-Mailroom eval / Langfuse dataset sync. *labels package / launcher draws* | DROP | - | - | - |
| `mailroom_ui/intake_normalize.py` | 78 | Deterministic intake normalize — mirror of llm-mailroom ``agents.intake``. *mirror of llm-mailroom code* | DROP | - | - | - |
| `mailroom_ui/langfuse_source.py` | 710 | Langfuse adapter — the sole source of truth for The-Mailroom. *"sole source of truth" today; must read the record* | REPLACED BY RECORD | `mailroom_launcher/floor/record_source.py` | mailroom_ui/models | langfuse |
| `mailroom_ui/metrics.py` | 169 | Aggregations over interpreted runs — every number from Langfuse data. *numbers come from the Dojo, not the UI* | REPLACED BY RECORD | Dojo reports | mailroom_ui/models, mailroom_ui/pipeline_schema | - |
| `mailroom_ui/models.py` | 222 | Pydantic models for The-Mailroom. *PipelineRun shape built from record replay timeline* | PORT AND DE-HARDCODE | `mailroom_launcher/floor/models.py` | - | pydantic |
| `mailroom_ui/multi_source.py` | 200 | Multi-source aggregator: serve several trace sources through one facade. *one source: the record* | REPLACED BY RECORD | - | mailroom_ui/models | - |
| `mailroom_ui/phoenix_source.py` | 525 | Arize Phoenix adapter — an ADDITIONAL local trace source. *reads mailroom-reloaded spans today* | REPLACED BY RECORD | - | mailroom_ui/models, mailroom_ui/pipeline_schema | phoenix |
| `mailroom_ui/pipeline_eval.py` | 73 | Offline eval adapters; Dojo owns quality scoring under the optional eval extra. *Dojo owns scoring* | DROP | - | - | llm_dojo_scoring |
| `mailroom_ui/pipeline_ops.py` | 241 | Read-only bridge to the llm-mailroom API (watcher + inbox). | PORT AND DE-HARDCODE | `mailroom_launcher/ops.py` (engine API bridge) | - | - |
| `mailroom_ui/pipeline_schema.py` | 678 | Pipeline topology and taxonomy for interpreting llm-mailroom traces. *topology/taxonomy mirror; `RELOADED_NODE_STAGES`* | BECOMES CONFIG | policy file (stations from policy nodes) | mailroom_ui/models | yaml |
| `mailroom_ui/producer.py` | 408 | Pinned llm-mailroom import surface (distribution name: ``mailroom``). *pinned producer surface* | DROP | - | mailroom_ui/pipeline_schema | pydantic |
| `mailroom_ui/prompt_registry.py` | 74 | Vendored mirror of llm-mailroom's agent prompts (the #1 maintenance duty). *vendored prompt mirror* | DROP | - | - | - |
| `mailroom_ui/reconsideration.py` | 256 | Objective review / reconsideration causes. *mirror of engine code* | DROP | - | mailroom_ui/pipeline_schema | - |
| `mailroom_ui/review_actions.py` | 834 | Operator write-path to llm-mailroom human review. *operator write-path to human review* | REVIEW STATION (other agent) | review station | mailroom_ui/pipeline_ops, mailroom_ui/pipeline_schema | - |
| `mailroom_ui/source_registry.py` | 149 | Trace-source registry (ADR-006, T-4). | REPLACED BY RECORD | - | - | - |
| `mailroom_ui/sources.py` | 65 | Shared trace-source contract for The-Mailroom. | REPLACED BY RECORD | - | mailroom_ui/models | - |
| `mailroom_ui/trace_cache.py` | 181 | Disk snapshot of Langfuse-derived runs (never fabricated). *record is the cache* | REPLACED BY RECORD | - | - | - |
| `mailroom_ui/trace_interpreter.py` | 910 | Trace interpreter: Langfuse trace + observations + scores -> PipelineRun. *trace+observations -> PipelineRun; replaced by record replay* | REPLACED BY RECORD | `mailroom_record` replay timeline + `mailroom_launcher/floor/` | mailroom_ui/models, mailroom_ui/pipeline_schema | - |

#### `operator_desk` (10 modules, 1,574 lines)

| Module | Lines | What it does | Verdict | Target | Depends on (to port first) | Third-party |
|---|---:|---|---|---|---|---|
| `operator_desk/__init__.py` | 10 | Operator desk — auth, archive, ops, and bin observer. | PORT AND DE-HARDCODE | `mailroom_launcher/desk/` | operator_desk/mount | - |
| `operator_desk/__main__.py` | 21 | ``python -m operator_desk`` — migrate the operator store and print status. | PORT AND DE-HARDCODE | `mailroom_launcher/desk/` | operator_desk/mount | dotenv |
| `operator_desk/archive.py` | 176 | Archive file access for the operator desk. *archive access via version chain blobs* | PORT AND DE-HARDCODE | `mailroom_launcher/desk/archive.py` | operator_desk/auth | fastapi, fitz, pydantic |
| `operator_desk/auth.py` | 316 | JWT auth for the operator desk. *JWT; overlaps review station auth* | PORT AS IS | `mailroom_launcher/desk/auth.py` | operator_desk/credentials | fastapi, jwt, pydantic |
| `operator_desk/credentials.py` | 53 | Pure credential policy shared by auth and empty-store bootstrap. | PORT AS IS | `mailroom_launcher/desk/credentials.py` | - | - |
| `operator_desk/db.py` | 254 | SQLite store for operator-desk tables (users, audit, archive index). *operator store (users, audit, archive index)* | REPLACED BY RECORD (users split out) | audit and archive index -> record index (it holds only what is in the log); users and credentials -> the desk's (or the review station's) own store, never the record index | operator_desk/credentials | bcrypt |
| `operator_desk/mount.py` | 97 | Mount the operator desk on the visualizer FastAPI app. | PORT AND DE-HARDCODE | `mailroom_launcher/desk/mount.py` | operator_desk/archive, operator_desk/auth, operator_desk/ops, operator_desk/websocket | fastapi |
| `operator_desk/observer.py` | 321 | Filesystem observer — bin moves become structured operator events. *filesystem bin observer* | REPLACED BY RECORD | record event tail | operator_desk/websocket | dotenv, watchdog |
| `operator_desk/ops.py` | 213 | Operator ops snapshot. | PORT AND DE-HARDCODE | `mailroom_launcher/desk/ops.py` | mailroom_ui/models, operator_desk/auth, operator_desk/websocket | fastapi, pydantic |
| `operator_desk/websocket.py` | 113 | Operator-desk WebSocket for filesystem / observer events. *feed from record events* | PORT AS IS | `mailroom_launcher/desk/websocket.py` | operator_desk/auth | fastapi |

#### `scripts` (15 modules, 4,126 lines)

| Module | Lines | What it does | Verdict | Target | Depends on (to port first) | Third-party |
|---|---:|---|---|---|---|---|
| `scripts/__init__.py` | 7 |  | DROP | - | - | - |
| `scripts/bump_producer_pin.py` | 247 | Move the reviewed llm-mailroom pin and vendor its display contract. *no pins* | DROP | - | - | - |
| `scripts/demo_pilot_run.py` | 266 | Stagger a fake Langfuse pilot so envelopes actually travel the conveyor. | PORT AND DE-HARDCODE | `mailroom_launcher` demo on a fake record | server/main | tests, uvicorn |
| `scripts/demo_review_tray.py` | 320 | Working REVIEW-tray demo: FakeClient Langfuse + in-process producer stub. | REVIEW STATION (other agent) | - | server/main | fastapi, tests, uvicorn |
| `scripts/demo_v030_cast.py` | 235 | Static FakeClient floor for v0.3.0 release stills and desk recordings. | DROP | - | server/main | tests, uvicorn |
| `scripts/eval_pipeline.py` | 550 | Score Langfuse ``document-pipeline`` traces against HF docclass ground truth. *scores Langfuse traces* | REPLACED BY RECORD | Dojo | - | dotenv |
| `scripts/export_corpus_catalog.py` | 149 | Export a slim catalog of Lucius-Morningstar/mailroom-dataset for the terminal GH Pages site + TUI. | DROP | - | - | - |
| `scripts/export_snapshot.py` | 254 | Export a static JSON snapshot of the current trace sources for GitHub Pages. | PORT AND DE-HARDCODE | `mailroom_launcher` static snapshot from record | mailroom_ui/models, mailroom_ui/pipeline_schema | dotenv |
| `scripts/publish_space.py` | 310 | Publish the live Observatory to a Hugging Face Docker Space. *HF Space* | OPEN (not in design) | - | - | huggingface_hub |
| `scripts/release.py` | 219 | Semver release workflow for The-Mailroom. | DROP | - | - | - |
| `scripts/render_tui_shots.py` | 61 | Render TUI frames against a running Mailroom API for README screenshots. | PORT AS IS | `packages/launcher/scripts/` | - | rich |
| `scripts/run_production_pilot.py` | 135 | Orchestrate a production Langfuse-traced HF subset pilot. *launcher run CLI* | DROP | - | - | dotenv |
| `scripts/seed_demo.py` | 1026 | Seed demo traces INTO Langfuse (env `demo`) for UI/UX play-testing. *writes to Langfuse* | DROP | - | - | dotenv, langfuse |
| `scripts/sync_pilot_dataset.py` | 215 | Mirror Lucius-Morningstar/mailroom-dataset INTO a Langfuse dataset. *hosted write* | DROP | - | - | dotenv, langfuse |
| `scripts/sync_prompts.py` | 132 | Push the vendored agent prompts into Langfuse Prompt Management. *hosted write* | DROP | - | - | dotenv, langfuse |

#### `server` (5 modules, 1,419 lines)

| Module | Lines | What it does | Verdict | Target | Depends on (to port first) | Third-party |
|---|---:|---|---|---|---|---|
| `server/__init__.py` | 1 | The-Mailroom HTTP server package. | PORT AND DE-HARDCODE | `mailroom_launcher/floor/server/` | - | - |
| `server/debug_log.py` | 94 | Server debug ring buffer — machine-readable request telemetry for agents. | PORT AS IS | `mailroom_launcher/floor/server/debug_log.py` | - | - |
| `server/hosted.py` | 21 | Public hosted edition entry point (Mailroom Observatory). *public Observatory* | OPEN (not in design) | - | server/main | - |
| `server/main.py` | 850 | The-Mailroom web server: Langfuse-backed read-only API + pixel-art UI. *Langfuse-backed read-only API -> record-backed* | PORT AND DE-HARDCODE | `mailroom_launcher/floor/server/app.py` | mailroom_ui/models, mailroom_ui/pipeline_ops, mailroom_ui/pipeline_schema, mailroom_ui/review_actions, operator_desk/auth, server/debug_log | dotenv, fastapi, starlette, uvicorn |
| `server/poller.py` | 453 | Background poller: Langfuse -> compact run snapshots -> WebSocket clients. *Langfuse poller* | REPLACED BY RECORD | `mailroom_launcher/floor/server/tail.py` (record tail) | mailroom_ui/classification, mailroom_ui/models, mailroom_ui/pipeline_ops | fastapi |

#### `tui` (6 modules, 2,442 lines)

| Module | Lines | What it does | Verdict | Target | Depends on (to port first) | Third-party |
|---|---:|---|---|---|---|---|
| `tui/__init__.py` | 1 | mailroom-tui — typed-command REPL console for The-Mailroom. *TUI starts from mailroom-reloaded (not available here)* | PORT AND DE-HARDCODE | `mailroom_launcher/tui/` | - | - |
| `tui/commands.py` | 723 | Typed-command registry for the mailroom-tui REPL. | PORT AND DE-HARDCODE | `mailroom_launcher/tui/commands.py` | tui/corpus | rich |
| `tui/corpus.py` | 258 | Corpus browser for the mailroom-tui REPL. | PORT AND DE-HARDCODE | `mailroom_launcher/tui/corpus.py` | - | - |
| `tui/mailroom_console.py` | 730 | The-Mailroom TUI console — a typed-command REPL over the pipeline. | PORT AND DE-HARDCODE | `mailroom_launcher/tui/console.py` | tui/corpus, tui/views | rich, websockets |
| `tui/repos.py` | 244 | Constellation repo browser for the mailroom-tui REPL. *constellation repo browser; one workspace* | DROP | - | - | - |
| `tui/views.py` | 486 | Rich renderers for the mailroom-tui console. | PORT AS IS | `mailroom_launcher/tui/views.py` | mailroom_ui/pipeline_ops, tui/corpus | rich |

#### The-Mailroom front-ends (JS/TS/HTML/CSS lines)

| Folder | Files | Lines | Verdict |
|---|---:|---:|---|
| `web/` (pixel-art floor: `floor.js`, `console.js`, `inspector.js`, `review.js`, `review_plan.js`, `dojo-update.js`, `api.js`) | 13 | 3,862 | PORT AND DE-HARDCODE as the launcher floor UI (L4), fed by the record-backed server; `review*.js` -> review station |
| `ui/` (React/TS operator desk: `PipelineBoard`, `DocumentCard`, `LoginForm`, stores) | 33 | 1,852 | launcher operator desk UI (L5) or DROP if the review station's UI covers it: owner decision |
| `hosted/` (public Observatory, HF Space) | 6 | 3,149 | OPEN (not in design; a hosted write to publish) |
| `terminal/` (static GitHub Pages terminal) | 4 | 2,283 | DROP |

## 2. Old Dojo: remaining work as Dojo chunks

Line counts are the old modules' lengths (section 1a), a rough size for the port; new code (comparison, projections,
stability, log health) has no old source and is sized separately. Chunk 1 (scoring core, 13 modules / 5,385 lines of
partly or wholly ported source) is done and not repeated.

| Chunk | Old modules | Old lines | Depends on | Parity evidence available |
|---|---|---:|---|---|
| **D1 Statistics** | `analysis/bootstrap`, `analysis/calibration` | 582 | nothing unported (calibration imports bootstrap) | Old tests `test_bootstrap.py` (6 tests), `test_calibration.py` (17). Both are pure functions, so a differential corpus captured from the old Dojo works as in chunk 1 (`packages/dojo/scripts/capture_old_dojo_scalar_cases.py` pattern). Golden reach is indirect: `scoring/tasks` imports bootstrap. |
| **D2 Flat metric table and number provenance** | `metrics/registry` (keep names, units, aggregation; drop tiers), `metrics/metric_meta` (data), `metrics/scorecard_honesty` (replaced by the "every number carries n, cost basis, label status" rule) | 3,372 | none | No score numbers move. Mechanical check: every metric name that `scores.json` suites emit (incl. `metric_names` of emit-only suites) is in the table. Behaviour checklist for the provenance rule: `test_registry.py` (16), `test_v018_scorecard_honesty.py` (13). `scorecard_honesty` is imported by 11 modules (`analysis/cost`, `failure_modes`, `serving`, `metrics/emitter`, `pipeline/archive`, `sandbox_scoring`, `schemas/maud_schema`, `scoring/content_scoring`, `decision_lanes`, `reviewer`, `suites`), so its fail-closed GT behaviour must land before them. |
| **D3a Schemas as configuration** | `schemas/extraction_schemas`, `cuad_schema`, `maud`, `maud_schema`, `corporate_record_schema`, `schema_contract`, `sorter_guide`, `v10_rows`, plus the leftovers of `corpus`, `equivalences` (predicates), `gt_metadata` (CUAD presence) | 4,922 | D2 (`maud_schema` imports `scorecard_honesty`) | Old tests `test_extraction_schemas.py` (19), `test_cuad_schema.py` (28), `test_maud_schema.py` (37), `test_maud_catalog.py` (35), `test_corporate_record_schema.py` (37), `test_schema_contract.py` (7), `test_corpus.py` (15); fixtures `maud_valid_classes.json`, `taxonomy_field_types.json`. Equality test: the loaded YAML equals a frozen JSON dump of the old Python tables (as chunk 1 did with `old_registry_field_types.json`). One schema file per class must be the single source read by labels, Dojo and engine specialists (R4); today `profile.yaml` (labels) and `scoring.yaml` (Dojo) each list fields, and the Dojo handoff still owes the test that they agree. |
| **D3b Tasks, suites, document scorers and the golden baseline** | `scoring/tasks`, `suites`, `content_scoring`, `asr`, `claims_consistency`, `extraction_metrics`, `extraction_scope`, `agent_rubrics`, `scoring/doc_scorers/*` (10), `pipeline/sandbox_scoring`, `sandbox_schema_adherence`, `pilot_labels` | 5,966 | D1, D2, D3a; `suites` also imports `analysis/serving` (the `local_vs_api` suite), so serving's comparison core must come forward from D5 or the suite waits | **`tests/fixtures/golden/scores.json`**: 21 suites (`arbiter` … `sorter_reviewer`, `local_vs_api`) and 22 task kinds (`cer` … `wer`), produced by `scripts/freeze_goldens.py` through `score_task` and `get_suite(name).score / score_document`. **`tests/fixtures/doc_scorers/goldens.json`**: 89 cases captured from the sandbox (`264153a5`, Dojo 0.26.0) with the `departures.json` overlay and `archive_block_before.json`. Old tests: `test_golden_scores.py` (14), `test_suites.py` (22), `test_tasks.py` (16), `test_content_scoring.py` (23), `test_asr.py` (9), `test_score_document.py` (25), `test_doc_scorers_types.py` (10), `test_doc_scorers_registry.py` (8), `test_doc_scorers_common.py` (3), `test_doc_scorer_fixtures.py` (3), `test_doc_scorer_departures.py` (4), `test_sandbox_scoring_adoption.py` (6), `test_chained_policy.py` (2). This is design proof item 1 ("golden score cases pass through unchanged"). R4: extend `test_new_document_type.py` to document dispatch. |
| **D4 Label layer** | `schemas/label_policy` (scorable-field rules stay in the Dojo; precedence and stale handling are already in `mailroom_labels.resolve` / `quality`), `correspondence_gt_audit`, `corporate_record_relabel` (verify against the review overlays, then drop) | 929 | labels package (merged into this branch in `7382eb6`), D3a | `test_label_policy.py` (24). Labels handoff: an independent resolver agreed on 33,716 fields. Also owed from the Dojo handoff: check `labels_interface` Protocols against the real `LabelSet` / `DocumentLabels` / `FieldLabel`. The sandbox scorable-field count is a stated source: **source not available here**. |
| **D5 Run layer** | `analysis/cost`, `serving`, `diagnostics`, `failure_modes`, `pipeline/pipeline_scoring` | 2,447 | record reading: sealed columns (Task 6, here) and the importer (Task 10, other agent); D1, D2 | `test_cost.py` (6), `test_serving.py` (19), `test_serving_usage_completeness.py`, `test_diagnostics.py` (6), `test_failure_modes.py` (5). Price tables become a price sheet in config. Sandbox run metrics, specialist and grid cards, serving parity: **source not available here**. |
| **D6 Document outcome and behaviour** | `pipeline/archive` (scoring block; its audit row is replaced by the record), `scoring/decision_lanes`, `scoring/reviewer`, `pipeline/pipeline_verdict` | 2,982 | D1, D2, D3b (`archive` calls `doc_scorers.dispatch`), D5; decision events in the record (R10) | `test_archive_scoring.py` (27), `tests/behavior/test_decision_lanes.py` (30), `test_reviewer_scoring.py` (29), `archive_block_before.json`. Sandbox archive gate and cause codes, routing audit: **source not available here**. Dojo PR #182 (slot summary) is not on the old `main` (head `23f8234` is #181); the design ports it after it merges. |
| **D7 Comparison, keep rule, projections, stability, cost truth, log health** | none in the old Dojo (new per design); old sources in llm-mailroom: `scripts/autoresearch_gate`, `hillclimb_eval`, `compare_runs`, `calibrate_thresholds`, `calibrate_field_scoring`, `write_pilot_report`, `pipeline/synthetic_records`, the objective half of `pipeline/policy_replay` | 2,325 (llm-mailroom) | D1, D5, D6; replicate runs for the noise floor | Synthetic records with a planted calibration curve (`synthetic_records.py`) as a property test. The sandbox keep rule and autoresearch scoring: **source not available here**. Projections have no old code: test the fit selection with synthetic data of known form, the five-point refusal and the 3x warning, and the 30-document floor from the systems review. |
| **Stage-2 gate** | - | - | D1 to D7, record importer | Design "Proof it is right": goldens unchanged; v1 re-scored to baseline 6 correct, 5 review, 7 unscorable, 7 wrong, replicate 5, 5, 8, 7, mean utility -0.101 and -0.140; slice-100 r2 to 40 of 55 wrong archives and 9 of 95 reviews. The slice-100 r2 run folder is still missing (design open question 1); v1 and v2 runs are in project files under `runs/autoresearch`. Then Sets A and C scored from current-stack runs. |

**Dojo order:** D1 -> D2 -> D3a -> (serving core) -> D3b -> D4 -> D5 -> D6 -> D7 -> stage-2 gate. D1, D2 and D3a need no
record work and can start now; D3b's golden parity is the first big proof. D4 can run in parallel with D3b once D3a lands.
D5 onward waits for the record importer.

Leaves to the engine or config from the old Dojo (943 lines): `pipeline/intake` (intake cleaning; llm-mailroom
`agents/intake.py` imports it), `prompts/__init__`, `prompts/alignment` (becomes an engine test, per the design), `prompts/lineage`.

## 3. llm-mailroom: engine, config, launcher, record, drop

Totals by destination (lines of old modules, from section 1b): engine 28,326, launcher 9,897, Dojo 2,325, configuration
5,290, replaced by the record 10,676, drop 13,097, open (owner decision) 9,034, review station (other agent) 1,479.

### Engine chunks (stage 3, after the go/no-go gate)

| Chunk | Old modules | Modules | Old lines | Proof it can show |
|---|---|---:|---:|---|
| E1 foundation (client, prompts loader, config, caps, guards, models) | `llm/client`, `llm/retry`, `llm/prompts`, `llm/prompt_sets`, `llm/vision`, `llm/quota`, `llm/provider_health`, `langchain_agents/base_agent`, `langchain_agents/mock`, `pipeline/config`, `pipeline/limits`, `pipeline/failures`, `pipeline/guards`, `pipeline/extraction_normalize`, `pipeline/text_cleaning`, `schemas/documents`, `schemas/manifest`, `schemas/matter`, `agents/source_windows`, `graph/attempts` | 20 | 4,272 | unit, fault and boundary tests; fake model; no provider names in code (R5) |
| E2 agents and handoffs | `agents/arbiter`, `agents/archivist`, `agents/base`, `agents/boss`, `agents/contracts_specialist`, `agents/corporate_records_specialist`, `agents/correspondence_specialist`, `agents/gmail_triage`, `agents/image_extractor`, `agents/insurance_claims_specialist`, `agents/intake`, `agents/judge`, `agents/merger_agreement_specialist`, `agents/pdf_transcriber`, `agents/relations`, `agents/reporter`, `agents/sorter`, `agents/sorter_reviewer`, `langchain_agents/sorter_agent`, `langchain_agents/specialist_agents`, `pipeline/relations`, `graph/state` | 22 | 6,965 | each agent on recorded inputs with the fake model; handoff events written to the record |
| E3 policy format, compiler, legacy policy, decision slots | `graph/build_graph`, `graph/route_spec`, `graph/routing`, `graph/decision_points`, `agents/decision_backend`, `agents/extraction_gate`, `pipeline/decision_review`, `pipeline/reconsideration` | 8 | 8,840 | `engine graph --policy legacy` draws the nodes and edges `route_spec.py` declares; mock end-to-end equals the old pipeline on the fixture set |
| E4 replay client | `pipeline/policy_replay` | 1 | 1,096 | router replay identical on every stored document-run |
| E5 queue, workers, throughput | `pipeline/watcher`, `scripts/bench_throughput`, `scripts/stress_test` | 3 | 4,195 | >= 249 documents a minute (fake model, durable writes); killed-worker resume without a repeated paid call |
| E6 API and exporters | `api/auth`, `api/main`, `audit/sinks`, `audit/sinks/langfuse`, `audit/sinks/otlp`, `observability/langfuse_setup`, `observability/masking`, `observability/tracing` | 8 | 2,958 | upload and review-resolve keep their shape; engine runs with no exporter |

Notes:

- **The graph is the risk the design names.** E3's old source is 8,840 lines; the design says "the current graph is
  about 8,900 lines" (`src/graph/` is 8,864 lines). `graph/route_spec.py` already declares every edge as data
  (12 conditional routes, plain edges for `intake`, `catalog_write`, `archive`, `audit_failed`) and validates it, so it
  is the natural seed for the policy file format; `graph/routing.py` (940 lines) holds the conditions router replay must
  reproduce.
- **Legacy parity hazards found:** `langchain_agents/skills/` (28 md files, 650 lines) is appended to system prompts, so it
  must be inside the hashed prompt set; `langchain_agents/memory.py` feeds an outcome ledger under `data/memory/` back into
  retry prompts with no enable flag found, which is state outside the record and breaks exact replay unless dropped or
  recorded; the effective prompts live mostly in code (`langchain_agents/prompts.py` 3,198 lines, `llm/prompt_doctrine.py`),
  not in `deploy_v1/*.txt` (6 agent files), so `config/prompts/deploy_v1/` must be materialised from code and the
  materialised text compared byte for byte with what the old code sends.
- **`legacy` = the 0.30.0 tag after #309 and #310** (design, "Calls"). This checkout is at `070663f` with pyproject
  version 0.9.3; which commit is the tag was not checked here.
- **Scoring leaves the graph:** `graph/scoring_nodes.py` and the `observability/*` Dojo adapters (bootstrap,
  classification_scoring, metrics, scoring_wiring, specialist_suites, suite_scoring, langfuse_field_scoring, scores,
  honest_gaps, local_eval_packs) drop; the Dojo scores from the record.

### Becomes configuration

| Old | New |
|---|---|
| `src/config/taxonomy.yaml` (885 lines, 27 sections) | split by section, see the table in 1b: `doc_classes` and `field_scoring` are already ported; `confidence` `by_class` thresholds -> client profile; the rest of `confidence`, `routing`, `reviewer`, `decision_points`, `relations`, `pipeline` -> `config/policies/legacy.*`; `agents` -> client profile / run spec agent-to-model map; model maps, gateway -> launcher backend config; `cost_models` -> price sheet; `run_limits` -> run spec caps; `prompt_set` -> run spec; `prompt_source`, `audit` drop |
| `src/llm/prompt_sets/`, `langchain_agents/prompts.py`, `llm/prompt_doctrine.py`, `langchain_agents/skills/` | `config/prompts/deploy_v1/` (one file per agent, skills and doctrine included), `config/prompts/catalog.yaml` |
| `src/llm/frozen_v1/` | `config/prompts/frozen_v1/` |
| `langchain_agents/prompt_candidates/corporate_flat9.py` | an overlay under `config/prompts/` (offline candidate, not a default) |
| `langchain_agents/cuad_maud.py`, `doc_inventories.py` | the same per-class schema files as Dojo D3a (`config/clients/mailroom-dataset/schemas/`) |
| `pipeline/relations_mode.py` | policy setting |

### Launcher (stage 4; most launcher source is the sandbox, **not available here**)

| Chunk | From llm-mailroom | Modules | Old lines |
|---|---|---:|---:|
| L1 run spec, draws, run CLIs | `pipeline/hf_corpora`, `pipeline/hf_corpus_loader`, `pipeline/live_manifest`, `scripts/prepare_samples`, `scripts/pull_live_manifest`, `scripts/run_pilot`, `scripts/run_hf_pilot`, `scripts/run_calibration_sweep`, `scripts/run_experiment`, `scripts/run_vision_sweep`, `scripts/run_agent_eval`, `observability/agent_eval`, `scripts/validate_pipeline`, `scripts/cutover` | 14 | 8,493 |
| L2 backends (python part) | `llm/providers`, `langchain_agents/openrouter_utils`, `scripts/smoke_modal_tiers`, `scripts/smoke_live_documents` | 4 | 1,404 |

Plus `deploy/` (1,693 lines in 16 files: Compose files, `modal_vllm.py`, LiteLLM, llamafile, HF Space). The run spec,
preflight, spend gate with initials, price sheet check and autoresearch driver come from the sandbox: **source not available here**.

### Replaced by the record

`audit/` (all but the exporter sinks), `storage/` (audit log, catalog, db, warehouse, relations ledger, migration),
`schemas/audit.py`, `schemas/run_record.py`, `graph/run_events.py`, `llm/result_cache.py` (resume skips recorded steps),
`pipeline/bins.py` (bins become queue states), scripts `verify_audit_chains`, `audit_reconcile`, `audit_report`,
`analyze_audit_db`, `export_warehouse`, `build_legacy_audit_fixture`, `recover_processing`. Importing the 0.30 deploy's
audit database comes after cutover (design).

### Drop (reason in the table)

LegalBench (11 modules, 1,463 lines; dropped by the design), `dojo_source` (no pins), `agents/bert_intake` (ML contract
stays on 0.30), the Langfuse / Braintrust / Phoenix setup and sync scripts (nothing reads traces back; hosted writes),
`graph/contract.py` and `scripts/export_contract.py` (replaced by `engine graph --policy`), release / bump / CI helper
scripts, `pipeline/env.py`, `pipeline/logging.py`, `pipeline/label_vocab.py` (replaced by `mailroom_common` and
`mailroom_dictionaries`), `storage/loop_bridge.py`, `relations_graph.py`.

### Open (not in the design; owner decision)

Gmail intake channel (`pipeline/gmail_intake.py` 2,722, `mail_guards`, `mail_outbox`, `scripts/gmail_smoke_test`), the SMS
pager and watchdog (`pager.py` 849, `pager_core.py` 834, `sms_notify`, `status_notify`, `watchdog`, `ops_monitor`),
`langchain_agents/classifier.py` (vision classifier, called lazily from `sorter_agent.py`), `memory.py`, `toolkit.py`,
`observability/extraction_gt.py` and `posthoc_gt.py` (regex post-hoc GT, not a label layer in R6), `relations_scan`,
`run_quality_judges`, HF Space publishing.

### Third-party dependencies (llm-mailroom `pyproject.toml`)

| Dependency | Fate |
|---|---|
| `langgraph`, `langchain-core` | engine core (design: LangGraph runs the compiled policy) |
| `langgraph-checkpoint-sqlite` | drop: the record is the only checkpoint, framework persistence off |
| `langchain-openai`, `openai` | engine model client (behind `mailroom_common.models`); provider-neutral |
| `fastapi`, `uvicorn`, `python-multipart`, `pydantic` | engine API (pydantic is already a record dependency) |
| `watchdog` | engine folder ingest |
| `sqlalchemy[asyncio]`, `aiosqlite`, `psycopg` (extra) | drop with the catalog/audit storage; the record index decides its own driver |
| `pypdf`, `pdfplumber`, `pymupdf`, `python-docx`, `reportlab`, `markdown-it-py` | engine procedural agents and ingest (`docx`/`reportlab` used in `build_graph`, `bins`, `watcher`, `review_resolve`, `api/main`, `prepare_samples`) |
| `pyyaml`, `httpx` | engine config / launcher draws |
| `langfuse`, `opentelemetry-sdk`, `opentelemetry-exporter-otlp-proto-http` | optional exporter extra only |
| `braintrust`, `openinference-instrumentation-openai`, `python-dotenv` | drop |
| `structlog` | drop: `mailroom_common.logging` (redacting) |
| `jellyfish`, `python-dateutil` | already Dojo core dependencies |
| `pyarrow` | already a record dependency (column files); the warehouse goes |
| `modal` (extra `deploy`) | launcher Modal backend extra |
| `scipy`, `sentence-transformers` (extra) | Dojo (scipy core, embeddings an injected optional backend) |
| `onnxruntime`, `tokenizers`, `huggingface-hub` (extra `bert`) | drop with `bert_intake` |

## 4. The-Mailroom: launcher operator surfaces

The-Mailroom is a trace consumer: `mailroom_ui/langfuse_source.py` calls itself "the sole source of truth", and
`trace_interpreter.py` turns a Langfuse trace plus observations plus scores into a `PipelineRun`. The design keeps the
floor, TUI and operator surfaces in the launcher and has them read the record; the replay timeline comes from the record
package (record Task 10b, not built yet). So:

- **Becomes launcher:** `server/` (FastAPI app and debug log; its Langfuse poller becomes a record tail), `tui/` (minus the
  constellation repo browser; the TUI base is `mailroom-reloaded`, **source not available here**), `operator_desk/`
  (auth, archive, ops, websocket; its audit and archive tables and filesystem bin observer are replaced by the record
  index and an event tail; its users and credentials keep their own store, never the record index), `web/` floor front-end, `mailroom_ui/classification.py`, `models.py`, `pipeline_ops.py`, and a few demo /
  snapshot scripts rebuilt over a fake record.
- **Replaced by the record:** every trace source (`langfuse_source`, `phoenix_source`, `multi_source`, `source_registry`,
  `sources`, `trace_cache`, `trace_interpreter`), `mailroom_ui/metrics.py` (numbers come from the Dojo), `scripts/eval_pipeline.py`.
- **Becomes config:** `pipeline_schema.py` (topology and taxonomy mirror; stations come from the policy file's nodes).
- **Drop:** vendored mirrors of llm-mailroom code and prompts (`docclass_prompts`, `prompt_registry`, `intake_normalize`,
  `reconsideration`, `producer`), Langfuse seeding and sync scripts (hosted writes), release and pin scripts, `terminal/`.
- **Review station (other agent):** `review_actions.py`, `demo_review_tray.py`, `web/js/review*.js`.
- **Open:** `hosted/` and `server/hosted.py` (public Observatory), `ui/` React desk if the review station UI covers it.
- **Design open question 2** ("Must The-Mailroom visualizer work against the new stack at cutover? If yes, the trace
  exporter has to emit the span names it reads today") is unanswered; if yes, E6's Langfuse exporter must emit the span
  names `pipeline_schema.py` and `trace_interpreter.py` expect.

## 5. Recommended build order across the remaining projects

The design's order: record -> labels -> Dojo -> prompts and mergers (no engine needed) -> engine "after the go or no-go
gate" -> launcher, backends, cutover. The gate, quoted from the design ("The go or no-go gate"):

> **Gate after stage 2:** JJB decides whether to build the new engine or fall back to approach 3 and slim the current
> pipeline in place. The engine stage is time-boxed. If router replay is not matching within the box, the fallback is taken
> and nothing built so far is wasted.

| # | Chunk | Project | Size (old lines) | Waits for | Proof it can show |
|---|---|---|---:|---|---|
| 0 | Record Tasks 7 to 12 (verify, index, trace merged in; importer, floor replay, pack/prune, stage-1 gate) | 1 (other agent) | - | - | Stage-1 gate: v1 and slice-100 r2 import, chains verify, trace works, size reported |
| 1 | D1 statistics | 3 | 582 | - | old tests + differential corpus |
| 2 | D2 flat metric table, number provenance | 3 | 3,372 | - | metric names in `scores.json` all present; provenance tests |
| 3 | D3a schemas as config (shared with engine) | 3 | 4,922 | D2 | each schema section equals its own old table: `scored` = old Dojo `doc_classes` and per-class profile tables (`23f8234`), `legacy` `extracted` = llm-mailroom `EXTRACTION_SCHEMAS` (`070663f`); labels/Dojo profile agreement test |
| 4 | D3b tasks, suites, doc scorers, golden harness | 3 | 5,966 | D1, D2, D3a, serving core | `scores.json` 21 suites + 22 task kinds identical; 89 doc-scorer goldens with the departures overlay (`tests/fixtures/doc_scorers/departures.json`, dojo 0.28.1) applied |
| 5 | D4 label layer | 3 | 929 | D3a, labels | `test_label_policy` cases; Protocol check against `LabelSet` |
| 6 | D5 run layer | 3 | 2,447 | record importer (0) | old tests; numbers on imported runs |
| 7 | D6 outcome and behaviour | 3 | 2,982 | D3b, D5, decision events | old tests; v1 outcome counts |
| 8 | D7 comparison, keep rule, projections, stability, cost truth, log health | 3 | 2,325 (+ new) | D5, D6 | synthetic planted curves; then stage-2 gate |
| 9 | **Stage-2 gate** | 3 | - | 1 to 8 | goldens unchanged; v1 6/5/7/7 and 5/5/8/7, utility -0.101 / -0.140; r2 40 of 55 and 9 of 95 (needs the r2 folder) |
| 10 | Prompt sets materialised to `config/prompts/deploy_v1/` (+ frozen_v1, skills, catalog) and the merger schema | 4 | 5,290 config (llm-mailroom modules) + prompt text files | D3a; can start after D3a | byte-identical rendered prompts against the old code for every agent and class; prompt alignment check |
| 11 | **Go / no-go gate** (owner decision) | - | - | 9 | - |
| 12 | E1 foundation | 5 | 4,272 | 11 | unit, fault and boundary tests; fake model |
| 13 | E2 agents and handoffs | 5 | 6,965 | E1, D3a, 10 | each agent against recorded inputs with the fake model; handoff events in the record |
| 14 | E3 policy format, compiler, `legacy` policy, decision slots | 5 | 8,840 | E2 | `engine graph --policy legacy` draws the same 22 nodes and edges as `route_spec`; mock end-to-end matches the old pipeline on the fixture set |
| 15 | E4 replay client and router replay | 5 | 1,096 | E3, record importer | router replay identical on every stored document-run (100 in v1, 95 in slice-100 r2), every mismatch listed |
| 16 | E5 queue, workers, leases, breakers, caps | 5 | 4,195 | E3 | >= 249 documents a minute with the fake model and durable writes; killed-worker resume |
| 17 | E6 API (upload, review-resolve with the review station) and exporters | 5 | 2,958 | E5, review station | API contract tests keep shape |
| 18 | L1 run spec, draws, run CLIs; L2 backends | 6 | 8,493 + 1,404 (+ 1,693 deploy/) + sandbox | E6 | a mock run per backend with no spend; paid parity runs started by the owner |
| 19 | L3 CLI and TUI, L4 floor server and web floor | 6 | The-Mailroom 4,302 port + 1,123 as is (+ front-ends) + sandbox + mailroom-reloaded (shared with 19b) | record Task 10b, E6 | floor replays an imported run through `build_timeline`-equivalent timeline |
| 19b | L5 operator desk | 6 | The-Mailroom `operator_desk/` (in the 19 total) | L4, review station | its own gate (design section 8): JWT refusal paths tested (missing, expired, wrong-role token); no document text in desk logs (log-scan test); desk store kept apart from the record index (boundary test); archive file access read-only (`fitz`) |
| 20 | Cutover: experiment policies B, D, E; Set F | 6 | - | all | Set F pass rule (design open question 5, approved) |

Notes on order:

- Steps 1 to 3 and 10 need no record work. Steps 1 and 2 can start at once; step 3 waits for D2 and step 10 for
  D3a, because the prompt alignment check reads the extracted schemas. D3a is on the critical path because the
  engine's specialists and the prompt sets read the same schema files.
- Steps 6 to 9 cannot start until the other agent's importer lands; the r2 numbers also need the missing slice-100 r2 folder.
- Engine work (12 to 17) is gated by step 11 and time-boxed by router replay (15).
- Every launcher chunk except the parts sourced from llm-mailroom and The-Mailroom needs the sandbox and mailroom-reloaded
  checkouts, which this session does not have; the launcher plan must inventory them first (R7).

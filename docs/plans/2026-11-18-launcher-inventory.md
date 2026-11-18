# Launcher inventory: the sandbox and mailroom-reloaded against the design

Date: 2026-11-18. Read-only investigation; nothing in any repository was changed.
Companion to `2026-11-18-workspace-port-inventory.md`, which covers the old Dojo, llm-mailroom and
The-Mailroom. Chunk names follow it: launcher L1 to L5 keep their meaning there, L6 to L9 are added
here; Dojo rows map onto D3b, D4, D5, D6 and D7.

**Superseded where it differs.** `2026-11-18-workspace-port-design.md` wins over this inventory's
verdicts, chunk order and module names. Since this was written: finding 4 is decided (owner decision 5:
the run driver L7 waits for the engine, no legacy driver); finding 3 is answered (the per-class noise
guard exists; D7 adds R2's per-field guard); every OPEN row and the R7 retirements are listed in the
design, sections 7 and 10, for the owner to confirm.

## Sources and method

| Source | Commit | Python modules (tests excluded) | Lines |
|---|---|---:|---:|
| `local-mailroom-sandbox` (`src/mailroom_sandbox`, `api-evals/`, `deploy/`, `scripts/`, `reports/dashboard/`; `vendor/` ignored) | `93f298c` | 207 | 79,218 |
| `mailroom-reloaded` (`src/`, `scripts/`, `deploy/`) | `2b79c28` | 104 | 29,470 |
| `mailroom-reloaded` browser TUI and `/ui` (`api/tui/`, `api/ui/`: JS, CSS, HTML, banners) | `2b79c28` | 31 files | 6,338 |
| Target `mailroom-dojo`, branch `claude/mailroom-dojo-workspace-port-ggh0x0` | `57610b7` | packages `common`, `dictionaries`, `record`, `labels`, `dojo`; no `launcher` yet | - |

- Lines are physical lines per file, from one AST walker (`launcher/walk_l.py`, `launcher/gen.py`, verdicts in
  `launcher/verdicts.py`; working scripts, not committed). Tests for scale: sandbox `tests/` 204 files / 47,567 lines;
  reloaded 106 Python test files plus 23 JS tests (`tests/tui/js/*.test.mjs`).
- "What it does" is the module docstring's first paragraph; my note follows in italics.
- "Depends on / must cut": `vendored:` = imports of the sandbox's vendored llm-mailroom (`pipeline:`) or old
  Dojo (`Dojo:`) trees, which do not exist in the workspace and must be replaced by the named engine/Dojo
  chunk; `cuts:` = imports of sandbox modules that go to a different destination (a launcher module importing
  a Dojo-bound or retired module, or a Dojo-bound module importing a launcher module, which R9 forbids).
  Retired modules show no cuts.
- Verdicts: **LAUNCHER** (keep, `packages/launcher`), **HANDS TO DOJO**, **RECORD** (replaced by the record or
  owned by the record package: importer, replay timeline, pack/prune), **CONFIG**, **RETIRE** (reason given).
  Added where needed: **ENGINE** (belongs to the engine, project 5), **LABELS** (labels package, project 2),
  **SPLIT** (one module, several owners, named), **OPEN** (owner decision), **REVIEW STATION** (other agent).
- Verdicts are proposals argued from the design ("The launcher and backends", "The Dojo", R7, R9, R10) and
  the workspace port design (`docs/plans/2026-11-18-workspace-port-design.md` section 4.4), not decisions.

### Not in scope here, and where it plugs in

- **Run log storage (other agent).** The importer reads the run folder described in 2.11; the floor calls the
  record's replay timeline (2.10). `job/checkpoint.py`, `node_trace.py`, `usage_capture.py`,
  `experiment_log.py`, `prompt_provenance.py`, `evidence.py`, `tui/floor_replay.py` are RECORD rows. Record on
  this branch has `blobs`, `hashing`, `log`, `models`, `seal`, `upgrade`; importer and replay timeline are not
  built (see `2026-11-18-workspace-port-inventory.md`).
- **Human review station (other agent).** Plug-ins: sandbox `review_cli.py` (`sandbox review list/show/resolve`);
  reloaded `review.py`, the `/v1/review/{doc_id}/resolve` route in `api/app.py`, and the TUI `review` and
  `resolve` commands in `api/tui/commands/pipeline.js`. The launcher TUI should load them as a plugin module
  from the review station (`plugins.js` `registerModule`), not own them.

## Totals by verdict

### Sandbox

| Verdict | Modules | Lines |
|---|---:|---:|
| LAUNCHER | 81 | 34,807 |
| RETIRE | 64 | 18,677 |
| HANDS TO DOJO | 27 | 9,030 |
| CONFIG | 11 | 5,191 |
| LABELS | 9 | 4,554 |
| SPLIT | 2 | 3,485 |
| RECORD | 7 | 1,902 |
| ENGINE | 5 | 1,277 |
| REVIEW STATION | 1 | 295 |
| **total** | 207 | 79,218 |

### mailroom-reloaded (Python)

| Verdict | Modules | Lines |
|---|---:|---:|
| RETIRE | 67 | 16,667 |
| OPEN | 16 | 6,188 |
| RECORD | 9 | 3,289 |
| SPLIT | 2 | 1,967 |
| LAUNCHER | 9 | 1,106 |
| REVIEW STATION | 1 | 253 |
| **total** | 104 | 29,470 |

## 1. Sandbox module table

Grouped by folder, in path order (`api-evals`, `deploy`, `scripts`, `src/mailroom_sandbox`, then `reports/dashboard`).

#### `api-evals/api_evals` (7 modules, 861 lines)

| Module | Lines | What it does | Verdict | Target | Depends on / must cut |
|---|---:|---|---|---|---|
| `__init__.py` | 0 |  *OpenRouter eval harness; an OpenRouter run spec replaces it* | RETIRE | - | - |
| `cost.py` | 188 | api_evals.cost — REAL OpenRouter API cost accounting. *real billed cost vs token estimate = "cost truth"* | HANDS TO DOJO | `packages/dojo/src/mailroom_dojo/cost_truth/` (D7) | - |
| `invoke.py` | 175 | api_evals.invoke — run an OpenRouter API eval task through the sandbox job machinery and return per-item rows + the experiment record + REAL cost. *OpenRouter eval harness; an OpenRouter run spec replaces it* | RETIRE | - | - |
| `ledger.py` | 108 | Tracked QWEN-flash cost ledger (issue #40). *OpenRouter eval harness; an OpenRouter run spec replaces it* | RETIRE | - | - |
| `registry.py` | 134 | api_evals — task registry for OpenRouter API eval comparisons. *OpenRouter eval harness; an OpenRouter run spec replaces it* | RETIRE | - | - |
| `report.py` | 210 | api_evals.report — markdown + JSON report generation for api-evals runs. *OpenRouter eval harness; an OpenRouter run spec replaces it* | RETIRE | - | - |
| `scoring.py` | 46 | api_evals.scoring — deterministic scoring reuse for api-evals runs. *OpenRouter eval harness; an OpenRouter run spec replaces it* | RETIRE | - | - |

#### `api-evals` (1 modules, 302 lines)

| Module | Lines | What it does | Verdict | Target | Depends on / must cut |
|---|---:|---|---|---|---|
| `run_api_evals.py` | 302 | api-evals CLI — run OpenRouter API eval comparisons. *OpenRouter eval harness; an OpenRouter run spec replaces it* | RETIRE | - | - |

#### `deploy` (5 modules, 2,716 lines)

| Module | Lines | What it does | Verdict | Target | Depends on / must cut |
|---|---:|---|---|---|---|
| `modal_gt_labeler.py` | 80 | Modal app for chunked v9.1 ground-truth labeling (SAND-045). | LABELS | with `gt_labeler` (OPEN) | - |
| `modal_job.py` | 405 | Remote job worker for the sandbox job CLI — `sandbox-job` Modal app (DMR-027, DMR-047). *L2 specialist-only job worker* | LAUNCHER | `packages/launcher/src/mailroom_launcher/backends/modal/job_app.py` | cuts: checkpoint (record), dependency_sources (retire), otel (retire) |
| `modal_vllm.py` | 837 | Modal-deployed vLLM for the local-mailroom-sandbox (KANBAN-064 sibling). *L2* | LAUNCHER | `packages/launcher/src/mailroom_launcher/backends/modal/vllm_app.py` | - |
| `publish_modal_image.py` | 28 | Build the vLLM serving image and publish it by name (`sandbox modal publish-image`). *L2* | LAUNCHER | `packages/launcher/src/mailroom_launcher/backends/modal/publish.py` | - |
| `run_gpu_cost.py` | 1,366 | GPU/MODAL cost-per-token driver (SAND-033) — the human-facing HITL script. *HITL GPU cost-per-token script* | SPLIT | driver -> launcher L2; throughput/cost curves -> Dojo D7 projections | - |

#### `scripts` (24 modules, 5,463 lines)

| Module | Lines | What it does | Verdict | Target | Depends on / must cut |
|---|---:|---|---|---|---|
| `build_a100_manifest.py` | 404 | build_a100_manifest.py — S1: per-leg A100 run manifest (DMR-078). *A100 study manifest* | RETIRE | - | - |
| `build_excluded_ids.py` | 129 | Build the committed held-out exclusion list from prompt-tuning runs. *L1 held-out exclusion list* | LAUNCHER | `packages/launcher/src/mailroom_launcher/draws/exclusions.py` | - |
| `calibrate_reviewer.py` | 361 | Pick each class's decision-reviewer cutoff from a labelled reviewer run. *per-class reviewer cutoffs* | HANDS TO DOJO | `packages/dojo/src/mailroom_dojo/confidence/thresholds.py` (D7); output is a policy file | vendored: Dojo:equivalences; cuts: checkpoint (record), overlay (config), paths (launcher), sand42_cards (retire), spec (launcher) |
| `check_cli_surface.py` | 124 | Generate/check the argparse-derived CLI reference without running commands. *L3 CLI reference check* | LAUNCHER | `packages/launcher/scripts/check_cli_surface.py` | - |
| `compare_sorter_prompt.py` | 461 | Validate a new sorter prompt against the saved V14 pass, scored on the verified targets. | HANDS TO DOJO | D7 comparison (prompt project 4) | vendored: Dojo:equivalences; cuts: paths (launcher) |
| `compare_specialist_prompt.py` | 89 | Compare a specialist prompt run with its reference run on the same documents (no model call). | HANDS TO DOJO | D7 comparison (prompt project 4) | cuts: paths (launcher), spec (launcher) |
| `estimate_spend.py` | 152 | Estimate the OpenRouter spend of planned runs and check it fits the key's remaining limit less the untouchable reserve (``spend.KEY_RESERVE_USD``). *L6 key-limit headroom* | LAUNCHER | `packages/launcher/src/mailroom_launcher/spend/estimate.py` | cuts: decision_backends (engine), metrics (split) |
| `export_sorter_handoff.py` | 120 | Save a sorter pass to the repository as a reusable handoff and sorter baseline. | LABELS | sorter baseline as a label snapshot (OPEN) | cuts: human_review (hands to dojo), metrics (split), paths (launcher) |
| `extraction_gate.py` | 599 | Extraction quality gate: build the baseline sample, then calibrate the gate against the dojo. | HANDS TO DOJO | D7 calibration (Set B offline) | vendored: Dojo:sandbox_scoring; cuts: checkpoint (record), cuad_scoring (retire), datasets (launcher), decision_backends (engine), interim_labels (labels), maud_scoring (retire), paths (launcher), spec (launcher) |
| `gen_maud_v2_prompt.py` | 81 | Generate ``config/prompts/merger_agreement_specialist_maud_v2.txt`` (SAND-042 RCA). | CONFIG | `config/prompts/` (prompt project 4) | vendored: Dojo:maud_schema |
| `gold_labels.py` | 615 | Work queue, labeler batches, validation and catalog build for gold labels. | LABELS | `packages/labels/scripts/` | vendored: llm_dojo_scoring, schemas; cuts: mailroom_sandbox (launcher) |
| `gt_label_ex10.py` | 504 | Label the v9.1 SEC EDGAR EX-10 gap in CUAD span form. | LABELS | `packages/labels/scripts/` | - |
| `interim_labels.py` | 308 | Build and check the interim ground-truth label overlay (``data/labels/interim/``). | LABELS | `packages/labels/scripts/` | cuts: checkpoint (record), mailroom_sandbox (launcher), spec (launcher) |
| `ladder_pair_report.py` | 37 | Paired control/treatment report for two finished ladder runs (offline re-score, no LLM). | HANDS TO DOJO | D7 paired report | cuts: mailroom_sandbox (launcher), spec (launcher) |
| `patch_sorter_handoff.py` | 390 | Verified targets for a saved sorter pass: correct labels, flag disputes, score the sorter. | LABELS | verified targets (OPEN) | cuts: paths (launcher) |
| `propagate_family_subagents.py` | 53 | Materialize + sync family subagents across mapped checkouts. *harness sync* | RETIRE | - | - |
| `refresh_api_prices.py` | 106 | Refresh ``config/api_prices.yaml`` from OpenRouter's public model catalog. *L6 price sheet check (`--check`, public catalog read)* | LAUNCHER | `packages/launcher/scripts/refresh_prices.py` | cuts: decision_backends (engine) |
| `refresh_decision_models.py` | 124 | Refresh ``config/decision_models.yaml``, the catalog of OpenRouter Decisions-API models. *L6* | LAUNCHER | `packages/launcher/scripts/refresh_decision_models.py` | - |
| `render_prompt_catalog.py` | 51 | Render docs/prompts/ — the catalog of every eval-environment prompt version. *prompt catalog renderer* | RETIRE | - | - |
| `smoke_campaign.py` | 121 | Offline pre-flight for an experiment day (SAND-081): no provider, deploy or spend. *L9 stage-4 mock gate seed: no provider, deploy or spend* | LAUNCHER | `packages/launcher/scripts/smoke.py` | - |
| `sync_requirements.py` | 186 | Generate requirements/*.txt from pyproject.toml extras (DMR-078). *per-package pyproject* | RETIRE | - | - |
| `sync_specialist_prompts.py` | 261 | Sync specialist production prompts into config/prompts/ (DMR-074 / SAND-026). | CONFIG | prompt chunk (materialise prompts once) | vendored: agents, pipeline:prompts, Dojo:prompts |
| `sync_subagents.py` | 41 | Regenerate harness stubs from config/subagents/family-roster.yaml. *harness sync* | RETIRE | - | - |
| `sync_vendor.py` | 146 | Refresh the sandbox vendor snapshots from the workspace packages (hub#62). *no vendor* | RETIRE | - | - |

#### `scripts/monorepo` (1 modules, 78 lines)

| Module | Lines | What it does | Verdict | Target | Depends on / must cut |
|---|---:|---|---|---|---|
| `after_packages_sync.py` | 78 | Post-hook for Digital-Mailroom ``scripts/sync_packages.py`` after pull/push. *hub sync hook* | RETIRE | - | - |

#### `scripts/sand032` (8 modules, 2,411 lines)

| Module | Lines | What it does | Verdict | Target | Depends on / must cut |
|---|---:|---|---|---|---|
| `capture_watch_screenshots.py` | 602 | Capture deterministic tray-TUI screenshots for the pretty-log docs. *synthetic data only* | LAUNCHER | `packages/launcher/scripts/` (L3 screenshot harness) | cuts: checkpoint (record) |
| `render_modal_performance.py` | 114 | Render Modal specialist performance SVGs (+ PNGs) under reports/dashboard/. *study* | RETIRE | - | - |
| `report.py` | 640 | SAND-032 report generator — every number is computed from recorded artifacts. *study* | RETIRE | - | vendored: llm_dojo_scoring |
| `rerender.py` | 136 | Re-lay committed SAND-032 figures with the current chart kit, from their own data. *study* | RETIRE | - | - |
| `rescore.py` | 107 | Re-score a finished run's items.jsonl with the current sandbox scorer (no LLM calls). *re-scoring is the Dojo over a record* | RETIRE | - | - |
| `scale.py` | 27 | Scale the WARM sandbox-vllm-sand032 fleet in place (no redeploy, no restart). *study fleet* | RETIRE | - | - |
| `spend.py` | 71 | SAND-032 spend ledger: GPU fleet windows (deploy → stop) × replicas × L4 rate. *study spend ledger* | RETIRE | - | - |
| `viz.py` | 714 | SAND-032 chart kit — /dataviz method, reference palette (validated light + dark). *study chart kit* | RETIRE | - | - |

#### `scripts/sand42` (2 modules, 533 lines)

| Module | Lines | What it does | Verdict | Target | Depends on / must cut |
|---|---:|---|---|---|---|
| `coverage-ledger.py` | 274 | SAND-042 coverage ledger: prove the designed metric matrix landed, per cell. *study* | RETIRE | - | - |
| `midrun_kv.py` | 259 | Mid-run vLLM /metrics — sample, then fold into the run cards. *study (vllm_metrics covers scraping)* | RETIRE | - | - |

#### `src/mailroom_sandbox (top level)` (46 modules, 20,064 lines)

| Module | Lines | What it does | Verdict | Target | Depends on / must cut |
|---|---:|---|---|---|---|
| `__init__.py` | 52 | Offline-first experiment harness with frozen family source selection. *rewrite: frozen family source selection retires* | LAUNCHER | `packages/launcher/src/mailroom_launcher/__init__.py` | cuts: runtime (retire) |
| `__main__.py` | 4 | `python -m mailroom_sandbox` runs the `sandbox` CLI. | LAUNCHER | `packages/launcher/src/mailroom_launcher/__main__.py` | - |
| `agent_routes.py` | 162 | Sandbox-owned per-agent transport adapter at the family's shared resolution seam. *L2; agent-to-model map comes from run spec + client profile, never code* | LAUNCHER | `packages/launcher/src/mailroom_launcher/backends/routes.py` | vendored: llm, pipeline:providers; cuts: components (config), overlay (config) |
| `api_floor.py` | 168 | Record a ``sandbox api eval`` report as a run the floor viewer can open. *every run is a record, so the floor opens any run; no special api-eval adapter* | RETIRE | - | - |
| `cli.py` | 4,660 | CLI: sandbox up/down/health/pilot/eval/matrix/... *L3; split 4,660 lines by command group; scoring commands call the Dojo* | LAUNCHER | `packages/launcher/src/mailroom_launcher/cli/` (one module per group) | vendored: Dojo:scorecard_honesty; cuts: checkpoint (record), dispatch (retire), doctor (retire), environment (retire), evidence (record), family (retire), family_api (retire), gate_render (hands to dojo) (+17 more) |
| `components.py` | 55 | Sandbox component gates (eval skip + taxonomy routing overlay). *eval skip + routing overlay become policy settings* | CONFIG | `config/policies/` (component gates) | cuts: paths (launcher) |
| `compose.py` | 92 | Docker Compose helpers for the sandbox stack. *L2* | LAUNCHER | `packages/launcher/src/mailroom_launcher/backends/compose.py` | cuts: overlay (config) |
| `context_fit.py` | 173 | Size whole-document agents' input budgets to their model's real context window. *L1; context windows are data in the backend model map* | LAUNCHER | `packages/launcher/src/mailroom_launcher/preflight/context.py` | - |
| `corpus.py` | 839 | Revision-pinned, deterministic HF corpus subsets + offline JSONL path (DMR-027). *L1 document draw; HF read only* | LAUNCHER | `packages/launcher/src/mailroom_launcher/draws/corpus.py` | vendored: Dojo:corpus |
| `dataset_registry.py` | 169 | Named datasets: the one registry a run spec points at (``config/datasets.yaml``). *L1* | LAUNCHER | `packages/launcher/src/mailroom_launcher/draws/registry.py` + `config/datasets.yaml` | - |
| `datasets.py` | 441 | Fixture catalog, tiny HF slices, and optional Hub pulls. *L1* | LAUNCHER | `packages/launcher/src/mailroom_launcher/draws/datasets.py` | - |
| `decision_backends.py` | 772 | Decision-model backends for the Lane A reviewer. *R10 slot backends incl. the Decisions endpoint; launcher keeps only the price check* | ENGINE | `packages/engine` decision slots (E3) + `config/decision_models.yaml` | vendored: pipeline:decision_backend, pipeline:sorter_reviewer, pipeline:config, pipeline:decision_review, pipeline:limits; cuts: paths (launcher) |
| `dependency_sources.py` | 199 | Run provenance and refusal of dependency source drift (no imports switched mid-run). *vendored-source drift guard; the record stores the code version instead* | RETIRE | - | - |
| `dojo_source.py` | 302 | Offline Dojo source policy: explicit source, matching install/cache, vendor. *no Dojo pin or vendor in the workspace* | RETIRE | - | - |
| `eval_environment_lineage.py` | 355 | eval-environment frozen v1 specialist prompt catalog (SAND-027-3). *prompt chunk (inventory section 5 step 10)* | CONFIG | `config/prompts/frozen_v1/` | cuts: paths (launcher) |
| `experiments.py` | 1,046 | Guided experimental designs composed from existing locked sandbox runs. *L8; design -> paired draw -> preflight -> initials -> smoke -> launch; its report step calls the Dojo* | LAUNCHER | `packages/launcher/src/mailroom_launcher/experiments.py` | cuts: checkpoint (record), decision_backends (engine), grid_cards (hands to dojo), metrics (split), otel (retire), overlay (config), runtime (retire), sand42_cards (retire) |
| `family_api.py` | 572 | One programmatic surface over the two family packages: the mailroom HTTP API and the Dojo. *"one surface over mailroom API and Dojo": the launcher imports the packages directly* | RETIRE | - | vendored: pipeline:build_graph, pipeline:bins, pipeline:config |
| `gold_labels.py` | 1,256 | Gold labels for unlabelled extraction fields: work queue, validation gate and catalog. *labels project 2; not launcher* | LABELS | `packages/labels` (check against `mailroom_labels`) | vendored: Dojo:schemas.cuad_schema, Dojo:schemas.schema_contract, Dojo:schemas.scored_enums, Dojo:scoring.suites, pipeline:documents; cuts: mailroom_sandbox (launcher), paths (launcher) |
| `gt_labeler.py` | 899 | Chunked ground-truth labeling for mailroom-dataset Hub tag v9.1. *model-labelled GT; real model calls need owner go-ahead* | LABELS | `packages/labels` / OPEN | vendored: pipeline:cuad_maud; cuts: metrics (split), spec (launcher) |
| `health.py` | 277 | Reachability probes for local OpenAI-compatible servers. *L2* | LAUNCHER | `packages/launcher/src/mailroom_launcher/backends/health.py` | cuts: overlay (config) |
| `interim_labels.py` | 382 | Interim ground-truth labels: the unpublished enhancements to the mailroom-dataset GT. *labels project 2* | LABELS | new label snapshot under `labels/` | vendored: Dojo:sandbox_scoring, Dojo:schemas.corporate_record_schema; cuts: paths (launcher) |
| `llm_timeout.py` | 59 | Honor ``run_limits.llm_call_timeout_seconds`` for the LangChain agents. *monkeypatch of vendored agents retires* | ENGINE | E1 caps (`run_limits` in the run spec) | vendored: langchain_agents |
| `mock_llm.py` | 136 | Deterministic fake OpenAI client for --mock pipeline / eval runs. *L2: the mock backend the stage-4 gate needs* | LAUNCHER | `packages/launcher/src/mailroom_launcher/backends/mock.py` (over the engine fake model) | vendored: Dojo:sandbox_scoring, pipeline:documents; cuts: schema_adherence (hands to dojo) |
| `modal_matrix.py` | 157 | Modal+vLLM model/GPU swap from ``config/models.yaml`` ``modal_models``. *L2* | LAUNCHER | `packages/launcher/src/mailroom_launcher/backends/modal/matrix.py` + `config/backends/modal-models.yaml` | cuts: overlay (config) |
| `modernbert.py` | 338 | Local mailroom-ml ModernBERT feeder (path contract, no weight copies). *ML contract stays on the 0.30 line (same call as `agents/bert_intake`)* | RETIRE | - | - |
| `overlay.py` | 406 | Taxonomy overlay + OpenRouter→local model map. *taxonomy overlay and OpenRouter->local map are data* | CONFIG | client profile + run spec agent-to-model map | vendored: pipeline:config; cuts: context_fit (launcher), paths (launcher) |
| `paths.py` | 125 | Filesystem roots for the local-mailroom-sandbox checkout. | LAUNCHER | `packages/launcher/src/mailroom_launcher/paths.py` | - |
| `pipeline_source.py` | 145 | Offline pipeline resolution: explicit source, matching install/cache, vendor. *vendored pipeline selection* | RETIRE | - | - |
| `ports.py` | 10 | Default port for the pipeline-floor viewer. *L4* | LAUNCHER | `packages/launcher/src/mailroom_launcher/floor/ports.py` | - |
| `prep.py` | 329 | Load / clean / prepare offline fixtures for notebook and CLI use. *L1* | LAUNCHER | `packages/launcher/src/mailroom_launcher/draws/prepare.py` | cuts: overlay (config), runtime (retire) |
| `prompt_catalog.py` | 539 | Render the eval-environment prompt catalog (``docs/prompts/``) from the dojo lineage. *prompt chunk* | CONFIG | `config/prompts/catalog.yaml` (renderer retires) | cuts: agents (launcher), mailroom_sandbox (launcher), paths (launcher) |
| `prompt_registry.py` | 502 | Pipeline-agent prompt registry + runtime overrides (DMR-027). *prompt chunk; Langfuse prompt source retires* | CONFIG | `config/prompts/<set>/` + engine prompt loader | vendored: pipeline:prompts, pipeline:prompts; cuts: paths (launcher), spec (launcher) |
| `prompt_versions.py` | 417 | Run sandbox experiments with ANY eval-environment prompt version. *prompt chunk* | CONFIG | run spec `prompt_set` + overlays | vendored: Dojo:prompts; cuts: mailroom_sandbox (launcher) |
| `prompts.py` | 53 | Prompt-variant overlay for local-model experiments. *prompt chunk* | CONFIG | `config/prompts/<set>/` overlays | cuts: paths (launcher) |
| `provider_credentials.py` | 57 | Make the LangChain agents send the RESOLVED provider's API key (SAND-038). *monkeypatch retires; keys from env only* | ENGINE | E1 model client | vendored: langchain_agents |
| `providers.py` | 109 | Named local serving profiles and URL construction (no sockets). *L2* | LAUNCHER | `packages/launcher/src/mailroom_launcher/backends/profiles.py` + `config/backends/profiles/` | cuts: overlay (config) |
| `report_paths.py` | 138 | Resolve report grouping from a run ID or its locked experiment metadata. *study report grouping* | RETIRE | - | - |
| `review_cli.py` | 295 | `sandbox review`: terminal human review over the mailroom API. *`sandbox review list/show/resolve`* | REVIEW STATION | other agent | vendored: pipeline:reconsideration; cuts: _exec (launcher), cli (launcher) |
| `runtime.py` | 519 | Activate a sandbox profile before any mailroom import does real work. *activates a profile by patching the vendored pipeline; the engine reads the run spec. See open question on a legacy driver* | RETIRE | - | vendored: pipeline:decision_backend, pipeline:extraction_gate |
| `sampling.py` | 308 | Run-scoped sampling for specialist calls. | ENGINE | E1 (run-scoped sampling knobs from the run spec) | vendored: pipeline:client, pipeline:retry, pipeline:limits |
| `serving_groups.py` | 206 | Group serving records into comparable legs (dojo v0.22 ``identity_mixed``). | HANDS TO DOJO | `packages/dojo/src/mailroom_dojo/run_layer/serving.py` (D5) | vendored: Dojo:serving |
| `snapshots.py` | 555 | Bounded, verified release snapshots. No network or writes during source selection. *vendor release snapshots* | RETIRE | - | - |
| `source_windows.py` | 81 | Widen the vendored judge/arbiter source excerpts for long-context routes. | ENGINE | E1 `source_windows` (policy knob) | cuts: context_fit (launcher) |
| `tunnel.py` | 301 | SSH local-port-forward lifecycle for remote serving profiles. *L2 local variant on a remote host* | LAUNCHER | `packages/launcher/src/mailroom_launcher/backends/tunnel.py` | - |
| `watch.py` | 1,136 | Tray TUI (`sandbox watch`) — mailroom-themed live view of a sandbox job run. *L3; reads the record tail instead of the run folder* | LAUNCHER | `packages/launcher/src/mailroom_launcher/tui/tray.py` | cuts: checkpoint (record), metrics (split) |
| `watch_target.py` | 268 | Resolve what `sandbox watch` / `sandbox web` should follow. *L3* | LAUNCHER | `packages/launcher/src/mailroom_launcher/tui/target.py` | cuts: checkpoint (record) |

#### `src/mailroom_sandbox/autoresearch` (10 modules, 3,269 lines)

| Module | Lines | What it does | Verdict | Target | Depends on / must cut |
|---|---:|---|---|---|---|
| `__init__.py` | 1 | Autoresearch: a config-only greedy search over run-spec levers (see docs/autoresearch.md). | LAUNCHER | `packages/launcher/src/mailroom_launcher/autoresearch/__init__.py` | - |
| `cli.py` | 154 | ``sandbox autoresearch plan\|step\|run\|status\|holdout``. *L8* | LAUNCHER | `packages/launcher/src/mailroom_launcher/autoresearch/cli.py` | - |
| `diagnose.py` | 156 | The weakness table: lost utility charged to a (document class, node). *lost utility by (class, node)* | HANDS TO DOJO | `packages/dojo/src/mailroom_dojo/compare/weakness.py` (D7) | - |
| `estimate.py` | 209 | Worst-case cost of a candidate round, before it runs. *L8; worst-case round cost before it runs (spend side, reads the price sheet)* | LAUNCHER | `packages/launcher/src/mailroom_launcher/autoresearch/estimate.py` | cuts: decision_backends (engine), metrics (split), scoring (hands to dojo) |
| `keep.py` | 86 | The keep rule: whether a candidate replaces the champion. *the keep rule* | HANDS TO DOJO | `packages/dojo/src/mailroom_dojo/compare/keep_rule.py` (D7) | - |
| `levers.py` | 596 | The lever catalog: one config change each, applied to a run spec as a dotted-path patch. *L8; patches become policy/run-spec overlays* | LAUNCHER | `packages/launcher/src/mailroom_launcher/autoresearch/levers.py` + `config/autoresearch/levers.yaml` | cuts: decision_backends (engine) |
| `loop.py` | 1,478 | The autoresearch loop: a greedy, config-only search over the lever catalog. *L8 driver; `noise_floor`, `class_noise`, `_gate_summary` call the Dojo instead* | LAUNCHER | `packages/launcher/src/mailroom_launcher/autoresearch/loop.py` | cuts: report (hands to dojo) |
| `parity.py` | 93 | Production parity: what the study's base spec shares with a production slice and where it differs. *spec-vs-production-slice diff (no scores)* | LAUNCHER | `packages/launcher/src/mailroom_launcher/autoresearch/parity.py` | - |
| `report.py` | 108 | ``report.md``: the study report (ledger, cost split, kept levers, production parity). *study report: ledger, cost split, kept levers* | HANDS TO DOJO | Dojo study report (D7); ledger view stays launcher | cuts: autoresearch (launcher), loop (launcher) |
| `scoring.py` | 388 | Per-document utility, cost by role, and the paired comparison of two runs on the same draw. *doc utility, RunScores, paired delta, cost by role* | HANDS TO DOJO | `packages/dojo/src/mailroom_dojo/outcome/utility.py` (D6) + `compare/paired.py` (D7) + `run/cost.py` (D5) | cuts: job (launcher), metrics (split) |

#### `src/mailroom_sandbox/eval` (15 modules, 5,435 lines)

| Module | Lines | What it does | Verdict | Target | Depends on / must cut |
|---|---:|---|---|---|---|
| `__init__.py` | 6 | Eval package. *package marker* | LAUNCHER | - | - |
| `agents.py` | 720 | Isolated eval registry for every live mailroom agent / graph node. *L7; isolated per-agent runs = the specialist-only run kind; rewrite against the engine API* | LAUNCHER | `packages/launcher/src/mailroom_launcher/run_kinds.py` (over engine agents) | vendored: pipeline:arbiter, pipeline:boss, pipeline:image_extractor, pipeline:intake, pipeline:judge, pipeline:pdf_transcriber, pipeline:reporter, pipeline:sorter, pipeline:sorter_reviewer, pipeline:build_graph, Dojo:intake; cuts: components (config), decision_backends (engine), runtime (retire), tracing (retire) |
| `cuad_scoring.py` | 18 | Compatibility adapter for the Dojo-owned sandbox CUAD benchmark. *adapter to Dojo-owned code (D3b owns it)* | RETIRE | - | vendored: llm_dojo_scoring, Dojo:sandbox_scoring |
| `experiment_log.py` | 101 | Append-only sandbox experiment log (JSONL + markdown summary). *append-only JSONL experiment log* | RECORD | replaced by the run record | vendored: Dojo:experiment; cuts: paths (launcher), prompt_versions (config), runtime (retire), scoring (hands to dojo) |
| `extraction_scope.py` | 271 | Scope extraction GT to the live class schema before scoring (SAND-026). *scope GT to the live class schema* | HANDS TO DOJO | `packages/dojo/src/mailroom_dojo/label_layer/scope.py` (D4) | vendored: Dojo:corporate_record_schema |
| `human_review.py` | 100 | Documents routed to human review: one metric, reported by every run type. | HANDS TO DOJO | `packages/dojo/src/mailroom_dojo/run_layer/review_rate.py` (D5) | vendored: pipeline:config |
| `matrix.py` | 160 | Provider × model × prompt experiment matrix. *L8* | LAUNCHER | `packages/launcher/src/mailroom_launcher/experiments.py` (matrix arms) | cuts: overlay (config) |
| `maud_scoring.py` | 23 | Compatibility adapter for the Dojo-owned sandbox MAUD benchmark. *adapter to Dojo-owned code (D3b owns it)* | RETIRE | - | vendored: llm_dojo_scoring, Dojo:sandbox_scoring |
| `primary.py` | 105 | The two primary tasks: ``specialist [TARGET]`` and ``pipeline [--class CLASS]``. *L1; the two run kinds (specialist, pipeline)* | LAUNCHER | `packages/launcher/src/mailroom_launcher/run_kinds.py` | - |
| `prompt_provenance.py` | 136 | Experiment-log prompt labels (issue #39 / DMR-053 / SAND-027-3). | RECORD | engine records prompt id + hash per call | vendored: pipeline:prompts; cuts: eval_environment_lineage (config), prompt_versions (config) |
| `runners.py` | 1,459 | Eval runners: sorter / extract / chained / pipeline / specialist. *eval runners sorter/extract/chained/pipeline/specialist; scoring calls move to the Dojo* | LAUNCHER | `packages/launcher/src/mailroom_launcher/driver/` (L7) | vendored: pipeline:build_graph, pipeline:base_agent, pipeline:mock, Dojo:emitter, pipeline:bins, pipeline:config; cuts: eval_environment_lineage (config), metrics (split), modernbert (retire), node_trace (record), prompt_provenance (record), runtime (retire), scoring (hands to dojo), usage_capture (record) |
| `schema_adherence.py` | 410 | Parse-failure / schema-adherence checks, separate from extraction scores. *`mock_llm` also imports it: the mock backend must not* | HANDS TO DOJO | `packages/dojo/src/mailroom_dojo/run_layer/schema_adherence.py` (D5) | vendored: Dojo:corporate_record_schema, pipeline:documents |
| `scoring.py` | 724 | Deterministic scoring via llm-dojo-scoring + local JSONL score sink. | HANDS TO DOJO | `packages/dojo/src/mailroom_dojo/document_scoring/` dispatch (D3b); JSONL score sink retires | vendored: llm_dojo_scoring, Dojo:extraction_metrics, Dojo:scorecard_honesty, Dojo:serving; cuts: cuad_scoring (retire), maud_scoring (retire), paths (launcher), report_paths (retire) |
| `serving_parity.py` | 470 | Grant-style cost-compare records → dojo-scorable serving records. | HANDS TO DOJO | `packages/dojo/src/mailroom_dojo/run_layer/serving_parity.py` (D5) | vendored: Dojo:scorecard_honesty, Dojo:serving; cuts: metrics (split) |
| `tracing.py` | 732 | Langfuse v4 data-model tracing for sandbox evals (family contract). *Langfuse/Braintrust tracing; exporter is optional engine E6, nothing reads it back* | RETIRE | - | vendored: Dojo:mailroom, observability |

#### `src/mailroom_sandbox/job` (41 modules, 21,871 lines)

| Module | Lines | What it does | Verdict | Target | Depends on / must cut |
|---|---:|---|---|---|---|
| `__init__.py` | 9 | Spec-driven job orchestrator for the sandbox (DMR-027). *package marker* | LAUNCHER | - | - |
| `api_budget.py` | 220 | API token spend: which models bill per token, their price bound, and the run ceiling (SAND-089). *L6 caps and price bound* | LAUNCHER | `packages/launcher/src/mailroom_launcher/spend/budget.py` | cuts: metrics (split) |
| `benchmark_check.py` | 706 | Loud reproducibility checks for Modal L4 Qwen specialist benchmarks. *L2; de-hardcode the Modal L4 Qwen study specifics or drop (owner)* | LAUNCHER | `packages/launcher/src/mailroom_launcher/preflight/modal_checks.py` | cuts: modernbert (retire), specialist_posture (config) |
| `bt_offline.py` | 98 | SAND-032: Braintrust-Experiment-shaped rows, written OFFLINE (no upload). *Braintrust rows (dropped)* | RETIRE | - | - |
| `budget_advice.py` | 169 | Recommended run ceilings, derived from the experiment design (SAND-089). *L6* | LAUNCHER | `packages/launcher/src/mailroom_launcher/spend/advice.py` | cuts: metrics (split) |
| `checkpoint.py` | 680 | Crash-safe, resumable run store for sandbox jobs (DMR-027). *`RunStore` defines the run-folder layout the importer reads* | RECORD | record log + resume; importer reads its layout | cuts: job (launcher), preflight (launcher), spec (launcher) |
| `container_telemetry.py` | 388 | SAND-042: GPU hardware telemetry sampled inside the Modal serving replica. *L2; GPU seconds and window fields on the record* | LAUNCHER | `packages/launcher/src/mailroom_launcher/backends/modal/telemetry.py` | - |
| `context_plan.py` | 332 | Preflight context plan: how a run's input budgets meet its prepared documents. *L1* | LAUNCHER | `packages/launcher/src/mailroom_launcher/preflight/context.py` | cuts: metrics (split), overlay (config), runtime (retire), source_windows (engine), specialist_posture (config) |
| `dated_reports.py` | 369 | Dated specialist report tree for new Qwen3-8B-AWQ (and later) cells. *study-specific report tree* | RETIRE | - | - |
| `deploy_env.py` | 210 | SAND-032: render the deploy env a run YAML implies, and diff it vs a shell. *L2* | LAUNCHER | `packages/launcher/src/mailroom_launcher/backends/modal/env.py` | cuts: runbooks (config), specialist_posture (config) |
| `evidence.py` | 58 | Run identity checks for append-only experiment evidence (no scoring logic). *run identity checks* | RECORD | record verify | cuts: runner (launcher) |
| `funding.py` | 60 | SAND-032: turn measured unit costs into funding line items. *unit costs -> funding lines* | HANDS TO DOJO | `packages/dojo/src/mailroom_dojo/projections/` (D7) | - |
| `gate.py` | 412 | Production slice gate: bars, per-document outcome and pooled rates for one finished run. *archive gate* | HANDS TO DOJO | `packages/dojo/src/mailroom_dojo/outcome/archive_gate.py` (D6); bars -> `config/clients/<client>/bars.yaml` | vendored: Dojo:schemas.doc_classes; cuts: paths (launcher) |
| `gate_audit.py` | 141 | Audit-trail check for the production slice gate: every decision links to its trace and archive. | HANDS TO DOJO | `packages/dojo/src/mailroom_dojo/log_health/audit_trail.py` (D7 log health), rebased on record verify | - |
| `gate_causes.py` | 258 | Cause buckets for the production slice gate: one primary cause per review, wrong archive and crash. *cause codes* | HANDS TO DOJO | `packages/dojo/src/mailroom_dojo/outcome/causes.py` (D6) | vendored: Dojo:schemas.corporate_record_schema, Dojo:schemas.scored_enums; cuts: cuad_scoring (retire) |
| `gate_compare.py` | 154 | Step-up verdict on the new documents and offline threshold evidence for the production slice gate. | HANDS TO DOJO | `packages/dojo/src/mailroom_dojo/compare/step_up.py` (D7) | cuts: job (launcher) |
| `gate_render.py` | 180 | Markdown rendering of a production slice gate report (``gate.md``); pure, no I/O. | HANDS TO DOJO | `packages/dojo/src/mailroom_dojo/reports/gate.py` (D6) | - |
| `grid_cards.py` | 1,304 | SAND-037 score & cost cards for the aligned specialist grid. *specialist + grid cards; de-hardcode SAND-037 postures* | HANDS TO DOJO | `packages/dojo/src/mailroom_dojo/run_layer/cards.py` (D5) | vendored: pipeline:specialist_agents, llm_dojo_scoring; cuts: agents (launcher), checkpoint (record), container_telemetry (launcher), job (launcher), paths (launcher), runbooks (config), runtime (retire), specialist_posture (config) |
| `grid_figures.py` | 536 | Figures for the specialist-grid master card (SAND-037 / SAND-039). *study figures; rebuild as Dojo reports when asked* | RETIRE | - | - |
| `grid_master.py` | 1,255 | Master score & cost card across the specialist-grid postures (SAND-037 / SAND-039 / SAND-040). *de-hardcode postures; scipy* | HANDS TO DOJO | `packages/dojo/src/mailroom_dojo/run_layer/cards.py` master view (D5) | cuts: agents (launcher), grid_figures (retire), paths (launcher), specialist_posture (config) |
| `grid_reader.py` | 552 | Plain-language reader report for the specialist-grid results (external audience). *study reader report* | RETIRE | - | - |
| `grid_reader_pdf.py` | 375 | Typeset PDF of the plain-language reader report (``READER-REPORT.pdf``). *study PDF* | RETIRE | - | - |
| `ledger.py` | 163 | Run ledger: who signed which paid run, when, and how it ended. *L6; signer goes on the Run; md ledger becomes a view over the record index* | LAUNCHER | `packages/launcher/src/mailroom_launcher/spend/ledger.py` | - |
| `metrics.py` | 2,119 | Serving metrics capture + local / Modal / API comparison (DMR-027). *2,119 lines, four owners* | SPLIT | price sheet -> `config/prices/` + launcher L6; serving/compare -> Dojo D5; extrapolate/estimate_suite -> Dojo D7; dataset/fleet locks -> launcher L1/L2 | vendored: Dojo:scorecard_honesty, Dojo:serving; cuts: checkpoint (record), container_telemetry (launcher), datasets (launcher), dated_reports (retire), decision_backends (engine), eval_environment_lineage (config), paths (launcher), report_paths (retire) (+3 more) |
| `node_trace.py` | 326 | Live per-node tracking for full-pipeline runs (SAND-076). *writes `nodes.jsonl` rows the importer and floor read* | RECORD | engine emits node events; `summarize` -> Dojo D5 | cuts: primary (launcher) |
| `otel.py` | 352 | OTLP trace-sink contract for sandbox jobs (DMR-027). *OTLP sink contract; optional exporter is engine E6* | RETIRE | - | - |
| `pair_report.py` | 196 | Paired control/treatment report for ladder runs, with and without templated GT fields. *gate imports its `_score`, `TEMPLATED_FIELDS`* | HANDS TO DOJO | `packages/dojo/src/mailroom_dojo/compare/paired.py` (D7) | cuts: runners (launcher) |
| `preflight.py` | 1,188 | Preflight: confirm, prepare, and lock a job run (DMR-027). *L1* | LAUNCHER | `packages/launcher/src/mailroom_launcher/preflight/` | vendored: llm_dojo_scoring, Dojo:experiment; cuts: checkpoint (record), decision_backends (engine), dependency_sources (retire), otel (retire), overlay (config), prompt_registry (config), prompt_versions (config), report_paths (retire) (+3 more) |
| `remote.py` | 491 | Remote Modal job execution for sandbox runs (DMR-027, DMR-047). *L2* | LAUNCHER | `packages/launcher/src/mailroom_launcher/backends/modal/remote.py` | cuts: checkpoint (record) |
| `routing_audit.py` | 260 | Post-run routing audit: did each escalation earn its cost, and are thresholds calibrated? | HANDS TO DOJO | `packages/dojo/src/mailroom_dojo/behaviour/routing_audit.py` (D6) | cuts: components (config), metrics (split), overlay (config), paths (launcher) |
| `runbooks.py` | 1,184 | Central operator runbooks (SAND-031). *mostly study runbooks; keep the reader, retire study entries* | CONFIG | `config/runbooks/` + thin `runbook` command | cuts: deploy_env (launcher), modal_matrix (launcher), paths (launcher), spec (launcher), suite (launcher) |
| `runner.py` | 1,446 | Checkpoint-aware item loop + scoring + experiment-log append (DMR-027). *item loop, resume, cancel; scoring + experiment-log append leave* | LAUNCHER | `packages/launcher/src/mailroom_launcher/driver/loop.py` (L7) | cuts: checkpoint (record), dated_reports (retire), dependency_sources (retire), grid_cards (hands to dojo), metrics (split), otel (retire), prompt_provenance (record), prompt_registry (config) (+4 more) |
| `sand42_cards.py` | 1,611 | SAND-042: per-cell scorecards and the paired cross-cell comparison. *SAND-042 study cards; its wilson/bootstrap/paired delta exist in Dojo D1/D7* | RETIRE | - | - |
| `sand42_figures.py` | 569 | SAND-042 figures, generated automatically after each run / per cell / for the master card. *study figures* | RETIRE | - | - |
| `spec.py` | 1,181 | Run-spec models and canonical hashing for the sandbox job CLI (DMR-027). *L1 run spec format and hashing* | LAUNCHER | `packages/launcher/src/mailroom_launcher/spec/` | cuts: decision_backends (engine), gold_labels (labels), interim_labels (labels), overlay (config), prompt_versions (config) |
| `specialist_posture.py` | 1,338 | Per-doc-type Modal L4 Qwen specialist posture (DMR-078). *per-class Modal L4 postures are data* | CONFIG | run-spec presets under `config/runs/` | - |
| `spend.py` | 255 | Explicit initials attestation bound to a verified run generation and its endpoints. *L6 initials gate* | LAUNCHER | `packages/launcher/src/mailroom_launcher/spend/gate.py` | cuts: checkpoint (record), metrics (split), otel (retire), overlay (config) |
| `suite.py` | 234 | Two-operator (and full) specialist suite manifests (DMR-077). *L1 suites of run specs* | LAUNCHER | `packages/launcher/src/mailroom_launcher/spec/suite.py` | - |
| `trace_pack.py` | 217 | Pack a run's local span mirror for upload, verify the copy, then prune. *span-mirror pack/prune; record pack/prune (record Task 11) replaces it* | RETIRE | - | - |
| `usage_capture.py` | 135 | Per-item token / TTFT capture helpers for the job runner. | RECORD | engine writes tokens/TTFT on events | vendored: pipeline:limits |
| `vllm_metrics.py` | 140 | SAND-032: scrape vLLM Prometheus ``/metrics`` through the Modal web URL. *L2* | LAUNCHER | `packages/launcher/src/mailroom_launcher/backends/vllm_metrics.py` | - |

#### `src/mailroom_sandbox/services` (10 modules, 1,562 lines)

| Module | Lines | What it does | Verdict | Target | Depends on / must cut |
|---|---:|---|---|---|---|
| `__init__.py` | 1 | Guarded wrappers around the external service CLIs (modal, vllm, hf, ...). *L2/L3 guarded service CLIs* | LAUNCHER | `packages/launcher/src/mailroom_launcher/services/__init__.py` | - |
| `_cli.py` | 53 | Small output helpers shared by the ``sandbox modal\|vllm\|hf`` command groups. *L2/L3 guarded service CLIs* | LAUNCHER | `packages/launcher/src/mailroom_launcher/services/_cli.py` | - |
| `_exec.py` | 177 | One guarded way to run an external CLI. *L2/L3 guarded service CLIs* | LAUNCHER | `packages/launcher/src/mailroom_launcher/services/exec.py` | - |
| `_redact.py` | 27 | Keep credentials in endpoint URLs out of diagnostics and published snapshots. *L2/L3 guarded service CLIs* | LAUNCHER | `packages/launcher/src/mailroom_launcher/services/redact.py` | - |
| `beacon.py` | 24 | Publish a service command as a ``mailroom.beacon/v1`` job so the board shows it. *L2/L3 guarded service CLIs* | LAUNCHER | `packages/launcher/src/mailroom_launcher/services/beacon.py` | - |
| `doctor.py` | 306 | ``sandbox doctor``: one status table for every service the sandbox talks to. *L2/L3 guarded service CLIs* | LAUNCHER | `packages/launcher/src/mailroom_launcher/services/doctor.py` | cuts: overlay (config), runtime (retire) |
| `hf_cli.py` | 181 | ``sandbox hf …``: guarded passthrough to the installed ``hf`` CLI. *L2/L3 guarded service CLIs; read-only Hub use, never push* | LAUNCHER | `packages/launcher/src/mailroom_launcher/services/hf.py` | cuts: runtime (retire) |
| `modal_cli.py` | 449 | ``sandbox modal …``: guarded passthrough to the real ``modal`` CLI. *L2/L3 guarded service CLIs* | LAUNCHER | `packages/launcher/src/mailroom_launcher/services/modal.py` | cuts: overlay (config), runtime (retire) |
| `poll.py` | 54 | Opt-in periodic doctor snapshots for the board; offline unless explicitly requested. *L2/L3 guarded service CLIs* | LAUNCHER | `packages/launcher/src/mailroom_launcher/services/poll.py` | cuts: runtime (retire) |
| `vllm_cli.py` | 290 | ``sandbox vllm …``: probe an endpoint, print or run the ``vllm serve`` command, scrape metrics. *L2/L3 guarded service CLIs* | LAUNCHER | `packages/launcher/src/mailroom_launcher/services/vllm.py` | cuts: overlay (config), runtime (retire) |

#### `src/mailroom_sandbox/subagents` (10 modules, 1,536 lines)

| Module | Lines | What it does | Verdict | Target | Depends on / must cut |
|---|---:|---|---|---|---|
| `__init__.py` | 17 | Central subagent roster + harness adapters (Cursor, OpenCode). *agent-harness roster sync across the old family; the workspace keeps `.claude/agents/`* | RETIRE | - | - |
| `dispatch.py` | 313 | Validate offline dispatch contracts and write durable, credential-free handoffs. *agent-harness roster sync across the old family; the workspace keeps `.claude/agents/`* | RETIRE | - | - |
| `doctor.py` | 466 | Harness health checks for OpenCode, Cursor, and family roster sync. *agent-harness roster sync across the old family; the workspace keeps `.claude/agents/`* | RETIRE | - | - |
| `environment.py` | 140 | Read-only execution identity and local harness configuration checks. *agent-harness roster sync across the old family; the workspace keeps `.claude/agents/`* | RETIRE | - | - |
| `family.py` | 77 | Family-wide subagent manifest loading and package filtering. *agent-harness roster sync across the old family; the workspace keeps `.claude/agents/`* | RETIRE | - | - |
| `materialize.py` | 98 | Copy family manifest + prompt snapshots into another package checkout. *agent-harness roster sync across the old family; the workspace keeps `.claude/agents/`* | RETIRE | - | - |
| `parse_opencode.py` | 34 | Parse OpenCode agent markdown (YAML frontmatter + body). *agent-harness roster sync across the old family; the workspace keeps `.claude/agents/`* | RETIRE | - | - |
| `propagate.py` | 159 | Materialize + sync subagents across family checkouts. *agent-harness roster sync across the old family; the workspace keeps `.claude/agents/`* | RETIRE | - | - |
| `roster.py` | 84 | Load and query the family subagent roster for a package. *agent-harness roster sync across the old family; the workspace keeps `.claude/agents/`* | RETIRE | - | - |
| `sync.py` | 148 | Render harness-specific agent stubs from the central roster. *agent-harness roster sync across the old family; the workspace keeps `.claude/agents/`* | RETIRE | - | - |

#### `src/mailroom_sandbox/tui` (14 modules, 6,673 lines)

| Module | Lines | What it does | Verdict | Target | Depends on / must cut |
|---|---:|---|---|---|---|
| `__init__.py` | 15 | Mailroom TUI design system, vendored from LLM-Mailroom-Services/mailroom-ml. *L3* | LAUNCHER | `packages/launcher/src/mailroom_launcher/tui/__init__.py` | - |
| `beacon.py` | 140 | mailroom.beacon/v1 — live progress heartbeats for long mailroom jobs. *L3 `mailroom.beacon/v1` heartbeats* | LAUNCHER | `packages/launcher/src/mailroom_launcher/tui/beacon.py` | - |
| `board.py` | 447 | `sandbox board` — one persistent mailroom display for every beacon job. *L3* | LAUNCHER | `packages/launcher/src/mailroom_launcher/tui/board.py` | - |
| `demo.py` | 210 | Synthetic mailroom run for the watch dev server (`sandbox dev`, `watch --web --demo`). *L3; synthetic run becomes a synthetic record* | LAUNCHER | `packages/launcher/src/mailroom_launcher/tui/demo.py` | cuts: checkpoint (record) |
| `floor_app.py` | 484 | `sandbox floor` — the pipeline-floor viewer as one resident app. *L4* | LAUNCHER | `packages/launcher/src/mailroom_launcher/floor/app.py` | cuts: checkpoint (record), floor_replay (record) |
| `floor_replay.py` | 466 | Provider-agnostic replay of a recorded pipeline run. *R7 contract; floor_app calls the record version* | RECORD | `mailroom_record` replay timeline (record Task 10b, other agent) | cuts: paths (launcher) |
| `pretty_log.py` | 2,367 | LLM Mailroom retro digital TUI for M9a training monitors (stdout only; never mutates logs). *L3; trim the M9a training-monitor parts (owner)* | LAUNCHER | `packages/launcher/src/mailroom_launcher/tui/pretty_log.py` | - |
| `services_panel.py` | 27 | Shared service status strip for the board and run watcher (published snapshots only). *L3* | LAUNCHER | `packages/launcher/src/mailroom_launcher/tui/services_panel.py` | - |
| `services_snapshot.py` | 71 | mailroom.services/v1 — the last `sandbox doctor` result, published for the job board. *L3* | LAUNCHER | `packages/launcher/src/mailroom_launcher/tui/services_snapshot.py` | - |
| `session.py` | 220 | Mailroom-themed terminal output for long sandbox CLI jobs (SAND-033). *L3* | LAUNCHER | `packages/launcher/src/mailroom_launcher/tui/session.py` | - |
| `shell.py` | 432 | Local interactive CLI; dispatches the existing parser, never a system shell. *L3; never a system shell* | LAUNCHER | `packages/launcher/src/mailroom_launcher/tui/shell.py` | - |
| `tray_context.py` | 515 | Tray TUI display context — job-aware labels for ``sandbox watch`` (terminal + web). *L3* | LAUNCHER | `packages/launcher/src/mailroom_launcher/tui/tray_context.py` | cuts: checkpoint (record) |
| `watchdog.py` | 283 | Watchdog for the Tray TUI (`sandbox watch`): turns run-store + log state into alerts. *L3 alerts from record + log state* | LAUNCHER | `packages/launcher/src/mailroom_launcher/tui/watchdog.py` | - |
| `web.py` | 996 | Browser-hosted Tray TUI (`sandbox watch --web`). *L3 browser tray; merge with the reloaded `/tui` server* | LAUNCHER | `packages/launcher/src/mailroom_launcher/tui/web.py` | cuts: checkpoint (record) |

#### `reports/dashboard` (13 modules, 6,444 lines)

| Module | Lines | What it does | Verdict | Target | Depends on / must cut |
|---|---:|---|---|---|---|
| `ale.py` | 130 | Accumulated Local Effects (ALE) on transparent surrogate models (numpy only). *study surrogate models* | RETIRE | - | - |
| `breakeven.py` | 156 | Modal L4 vs hosted API, per document: break-even volumes and the scenario where Modal wins. | HANDS TO DOJO | D7 projections (Modal vs API break-even) | - |
| `build_floor_digest.py` | 1,067 | Rebuild the pipeline-floor digest of ALL previous runs from on-disk artifacts. *L4; rebuild the digest from the record index* | LAUNCHER | `packages/launcher/src/mailroom_launcher/floor/digest.py` | - |
| `build_hub.py` | 398 | Build the Mailroom reports hub (``mailroom-reports.html``). *mailroom-issues hub* | RETIRE | - | - |
| `export_hub_reports.py` | 792 | Write the cross-repo cost comparison and master reports for the mailroom-issues hub. *hub* | RETIRE | - | - |
| `export_pngs.py` | 89 | Render every report figure in mailroom-issues to a presentation-ready PNG. *hub* | RETIRE | - | - |
| `gpu_report.py` | 842 | Modal + vLLM GPU economics report for the mailroom-issues hub (SAND-032, Qwen3-8B-AWQ on Modal L4). *study (tables -> Dojo serving reports)* | RETIRE | - | - |
| `hub_extract.py` | 808 | Source-verified extraction for the Mailroom reports hub. *hub* | RETIRE | - | - |
| `legacy_runs.py` | 417 | Source-verified extraction for the 16–27 Sep specialist runs (pre-SAND-032). *hub* | RETIRE | - | - |
| `modal_performance.py` | 434 | Modal specialist experiment visuals — drawn from committed run reports + serving exports. *study* | RETIRE | - | - |
| `pages_site.py` | 235 | Static GitHub Pages site for LLM-Mailroom-Services/mailroom-issues (served from ``main`` → ``/docs``). *hub Pages site* | RETIRE | - | - |
| `selftest_floor_digest.py` | 744 | Deterministic gate for build_floor_digest.py (stdlib only, plain asserts). | LAUNCHER | `packages/launcher/tests/` (L4) | - |
| `source_charts.py` | 332 | Redraws of the seven source-repo charts the mailroom-issues reports embed, in the sandbox chart kit. *hub* | RETIRE | - | - |

#### Sandbox `config/` (data, not modules; files / lines)

| Path | Files | Lines | Verdict | Target |
|---|---:|---:|---|---|
| `config/runs/` (incl. `legacy/`) | 225 | 16,354 | CONFIG | `config/runs/` in the new run-spec format (L1); convert the ones still used, archive `legacy/` |
| `config/prompts/` | 43 | 2,658 | CONFIG | `config/prompts/<prompt_set>/` (prompt chunk, inventory step 10) |
| `config/decisions/` | 11 | 1,612 | CONFIG | `config/policies/` decision-slot settings (R10) |
| `config/runbooks/` | 2 | 1,349 | CONFIG / RETIRE | study runbooks retire; keep the format if the `runbook` command stays |
| `config/experiments/` | 7 | 1,021 | CONFIG | experiment arms = named policy + config (L8) |
| `config/production/` (`bars.yaml`, slices) | 4 | 1,006 | CONFIG | `config/clients/<client>/bars.yaml` (read by Dojo D6) |
| `config/autoresearch/` (`levers.yaml` 495, studies `mailroom-v1`, `mailroom-v2`, `mock-smoke`) | 8 | 898 | CONFIG | `config/autoresearch/` (L8); `mock-smoke` is the zero-spend proof study |
| `config/subagents/` | 5 | 317 | RETIRE | harness roster |
| `config/profiles/` | 9 | 281 | CONFIG | `config/backends/profiles/` (L2) |
| `config/mailroom.taxonomy.base.yaml` 376, `taxonomy.overlay.yaml` 142 | 2 | 518 | CONFIG | client profile (`doc_classes` already ported) + policy `legacy` |
| `config/models.yaml` | 1 | 367 | CONFIG | backend model map (`modal_models`, local map) |
| `config/decision_models.yaml` | 1 | 234 | CONFIG | Decisions-API model catalog (engine E3 + price check L6) |
| `config/api_prices.yaml` | 1 | 184 | CONFIG | the price sheet, `config/prices/` (created by Dojo D5; L6 adds these entries; Dojo D7 cost truth reads it) |
| `config/components.yaml` 64, `datasets.yaml` 42, `report-groups.yaml` 16 | 3 | 122 | CONFIG / RETIRE | `config/datasets.yaml` (L1); components -> policy; report groups retire |
| `config/.env.example`, `README.md` | 2 | 276 | - | `.env.example` per package; never `.env` |

New top-level config folders (`config/runs/`, `config/backends/`, `config/datasets.yaml`, `config/autoresearch/`,
the price sheet `config/prices/`, which Dojo D5 creates with its map line) need a line in the layout map (R9) before
they are created.

#### Sandbox `deploy/` non-Python

| File | Lines | Verdict |
|---|---:|---|
| `deploy/docker-compose.yml` | 328 | LAUNCHER L2, but see finding 1: its services are `ollama`, `vllm`, `llamacpp`, `jupyter`, `phoenix` and a Langfuse stack (`postgres`, `clickhouse`, `redis`, `minio`, `langfuse-worker`, `langfuse-web`); no producer image |
| `deploy/Dockerfile` | 73 | LAUNCHER L2 (launcher image) |
| `deploy/teardown_vllm.sh` | 95 | LAUNCHER L2 |
| `deploy/htcondor/` (5 files) | 651 | OPEN (an HTCondor batch backend is not in the design's four) |
| `deploy/launchd/com.mailroom.board.plist` | 24 | OPEN (macOS autostart of `sandbox board`) |
| `deploy/conda/environment.yml` | 29 | RETIRE |
| `deploy/README.md` | 418 | rewrite as `packages/launcher/README.md` sections |

#### Sandbox floor front-end (`reports/dashboard/`, non-Python)

`floor.template.html` (529 lines) is the floor page `tui/floor_app.py:39` serves (`PAGE = "floor.template.html"`);
`floor_digest.js` is empty (0 lines; generated), `floor_digest.json` 1,195 lines generated. Verdict LAUNCHER L4.
The hub artefacts (`hub.template.html` 1,201, `mailroom-reports.html`, `hub_data.json`, figures) RETIRE.

## 2. Where each named capability lives (sandbox `93f298c`)

Paths are relative to the sandbox root; `ms/` = `src/mailroom_sandbox/`. Line ranges are AST `lineno`-`end_lineno`.

### 2.1 Run spec format -> LAUNCHER L1

- `ms/job/spec.py` (1,181). `RunSpec` 840-979: `schema_name` (YAML key `schema`), `run_id`, `runbook_id`, `task`,
  `target`, `profile`, `prompt` (`PromptRef` 87-113 per agent + default), `dataset` (`DatasetSpec` 116-357: provider,
  repo, config, split, revision, limit, sample_seed, strata, exclusions, interim / near-gold labels, draw, columns,
  local_path, source_sha256), `engine` (`EngineSpec` 522-536: `kind` in `vllm-local | vllm-remote | modal-vllm |
  openrouter`, model, `VLLMSpec` 360-441, `ModalSpec` 444-497), `job` (`JobSpec` 617-695: `mode` endpoint|modal,
  task, mock, checkpoint_every, max_retries, fail_fast, concurrency, **`cost_cap_usd`**, **`max_wall_seconds`**,
  **`api_cost_cap_usd`**, blind_routing, pipeline_handoff), `trace` (`TraceSpec` 776-782), `note`, `agent_models`,
  `agent_knobs`, `agent_routes` (`AgentRoute` 539-567), `reviewer` (`ReviewerSpec` 698-773), `extraction_gate`
  (785-802), `confidence`.
- Hashing: `spec_core` 1016-1055, `spec_hash` 1069-1071, `core_hash` 1074-1077; ids `resolve_run_id` 1088-1093;
  folders `runs_root` 1096-1099 (`data/runtime/runs/`), `run_dir` 1102-1105; `load_run_spec` 1108-1113;
  endpoint resolution `engine_base_url` 1141-1177.
- Gap against the design: the sandbox spec has `engine.kind` and per-agent routes but **no policy name/hash and no
  "kind = specialist-only | full pipeline" field**; the run kind is `task` (`ms/eval/primary.py`: `specialist`,
  `pipeline`) and the routing behaviour comes from the taxonomy overlay. The new spec adds `policy`, `kind`,
  `backend`, label snapshot ids and the signer (design "What a record holds", Run row).
- Examples: 225 YAML files under `config/runs/` (e.g. `config/runs/pipeline-luna-qwen-smoke.yaml`: schema, run_id,
  task, profile, prompt, dataset, engine, agent_routes, agent_knobs, reviewer, job, trace).

### 2.2 Preflight -> LAUNCHER L1

- `ms/job/preflight.py` (1,188): `preflight` 780-820 (takes the writer and control locks; `dry_run` read-only;
  `busy_refused` when a writer holds the lock), `_preflight` 823-1188. Named checks: `lock` (856), `prompt` (871-875),
  `report_group` (882), `generation` (983), `engine_spec` (990-1052), `dataset` (1010-1025), `target` (1038-1040),
  `engine_probe` (1059; `_probe_engine` 450-538, cold boot `record_cold_boot` 556-577), `trace_sink` (1092-1105),
  `modal_spec` (1110+), `context_plan` (`_record_context_plan` 592-615; plan in `ms/job/context_plan.py` 332 lines),
  `checkout` (`_checkout_check` 690-702, foreign-store takeover 625-651), reviewer and Decisions checks 104-335.
- It writes `spec.lock.json`, `prompt.lock.json` and `dataset.jsonl` (the locked draw) into the run folder.
- Cuts needed: imports `routing_audit` (Dojo), `runtime`, `dependency_sources`, `otel`, `report_paths` (retire),
  `overlay`, `prompt_registry`, `prompt_versions`, `specialist_posture` (config), `llm_dojo_scoring.experiment`.

### 2.3 Spend gate with initials and caps -> LAUNCHER L6

- `ms/job/spend.py` (255): `authorize` 196-214, `_authorize` 217-255. Receipt `spend-authorization.json` in the run
  folder (schema `mailroom.spend-authorization/v1`, mode 0600), valid for 24 h (`MAX_AGE_S`) and only while the
  review fingerprint matches; initials must match `[A-Z]{2,6}`; refuses without `--initials` when stdin is not a
  TTY; prints the full review and the attestation sentence before asking; appends a `spend_authorized` event.
  `paid` 35-42, `review_settings` 45-87 (what is shown and fingerprinted), `require_budget` 109-124,
  `key_reserve_usd` 144-150 and `require_key_headroom` 170-193 (OpenRouter key limit less an untouchable reserve).
- Caps: `JobSpec.cost_cap_usd`, `api_cost_cap_usd`, `max_wall_seconds` (2.1); `ms/job/api_budget.py` (220)
  `price_bound` 89-101, `unpriced` 83-86, `budget_errors` 104-124, `ApiBudget` 127-181; `ms/job/budget_advice.py`
  `recommend` 99-148; autoresearch study budget `Budget` (`ms/autoresearch/loop.py` 74-88) and reservations
  `_reserve` 848-857.
- Ledger: `ms/job/ledger.py` (163) `record_signed` 88-109, `mark_result` 112-132, `close_run` 135-148; a Markdown
  table tracked at `reports/run-ledger.md` (columns signed_utc, initials, action, run_id, config, spec, rows,
  api_cap_usd, result). In the record this becomes the Run's "who signed it" plus an index view.
- `scripts/estimate_spend.py` (152): planned-run spend against the key's remaining limit.

### 2.4 Price sheet check -> LAUNCHER L6 (sheet is CONFIG, also read by Dojo D5)

- `config/api_prices.yaml` (184 lines; `source` OpenRouter public catalog, `checked: '2026-11-01'`, USD per 1M tokens,
  tiers by `min_prompt_tokens`, cache read/write). Fallback copy `API_PRICE_SHEETS` in `ms/job/metrics.py` (~91-116,
  `API_PRICES_CHECKED = "2026-11-01"`).
- Readers: `ms/job/metrics.py` `api_price_sheets` 117-139, `price_sheet` 171-188 (a Decisions `:variant` is priced
  only from an explicit row, never by prefix), `call_price` 191-201, `call_cost` 204-210, `token_price_ceiling`
  427-438, `token_price` 441-471; GPU side `gpu_usd_per_hour` 245-262, `lock_gpu_fleet` 328-345.
- Check: `scripts/refresh_api_prices.py` (106) `--check` reports drift against the public catalog;
  `scripts/refresh_decision_models.py` (124) for `config/decision_models.yaml`. An unpriced model fails the budget
  check (`api_budget.unpriced`). Proposal: drop the in-code fallback table; a missing sheet is a preflight failure.

### 2.5 Modal and Compose deploy helpers -> LAUNCHER L2

- Modal serving: `deploy/modal_vllm.py` (837) `build_vllm_command` 525-575, `serve` 645-704, GPU telemetry sampler
  467-510, `download_model` 753-767, `_smoke_check` 774-803; `deploy/publish_modal_image.py` (28).
- Modal job worker (specialist-only runs on Modal): `deploy/modal_job.py` (405) `run_job` 201-382;
  `ms/job/remote.py` (491) `fire` 174-229, `is_alive` 232-249, `read_progress` 286-331, `pull_run_dir` 334-421,
  `cancel` 424-439, `ensure_running` 442-491.
- Around them: `ms/services/modal_cli.py` (449: `deploy`, `publish-image`, `prewarm`, `status`, `stop`, `logs`, `url`,
  `raw`), `ms/modal_matrix.py` (157, model/GPU swap from `config/models.yaml`), `ms/job/deploy_env.py` (210),
  `ms/job/container_telemetry.py` (388), `ms/job/vllm_metrics.py` (140), `ms/job/benchmark_check.py` (706).
- Compose: `ms/compose.py` (92) `compose_argv` 31-38, `default_profiles_for` 41-44, `run_compose` 47-55,
  `pull_ollama_models` 58-92, over `deploy/docker-compose.yml` (see finding 1). Local variant:
  `ms/providers.py` (109), `ms/health.py` (277), `ms/tunnel.py` (301), `ms/services/vllm_cli.py` (290).

### 2.6 Autoresearch loop: driver vs scoring and keep rule

- **Driver (LAUNCHER L8):** `ms/autoresearch/loop.py` (1,478) `Study` 99-177, `CliExecutor` 214-269, `Ledger`
  311-334, `spent_usd` 395-409, `propose` 449-465, `Loop` 523-1322 (`ensure_baseline` 910-938, `ensure_replicate`
  940-991, `plan` 994-1012, `step`/`_step` 1015-1058, `_candidate_round` 1108-1158, `run` 1161-1175, `holdout`
  1217-1275); `levers.py` (596) + `config/autoresearch/levers.yaml` (495); `estimate.py` (209)
  `estimate_round_cost` 114-158; `cli.py` (154: `plan`, `step`, `run`, `status`, `holdout`); `parity.py` (93).
- **Scoring (HANDS TO DOJO):** `ms/autoresearch/scoring.py` (388) `UtilityWeights` 61-68 (review -0.25, wrong
  archive -1, crashed -1, judge_error -1, unscorable 0), `doc_utility` 71-79, `RunScores` 103-200, `agent_cost`
  225-241, `score_run` 263-313, `paired_delta` 343-377 (paired bootstrap), `usd_per_utility_point` 380-388;
  `diagnose.py` (156) weakness table; `report.py` (108).
- **Keep rule (HANDS TO DOJO D7):** `ms/autoresearch/keep.py` (86) `decide_keep` 34-86: CI low above
  `max(0, noise_floor)`; no guarded class drops more than `max(max_class_drop, class_noise[cls])`; wrong archives
  not higher; cost per doc <= 1.25x unless gain >= 0.05; cost-goal levers use non-inferiority (-0.02) and a 0.9x
  saving; an unpriced candidate model is reverted. Noise inputs come from the driver: `Loop.noise_floor` 637-646,
  `Loop.class_noise` 648-652, `_guard` 654-692 (they should become Dojo calls on replicate records).

### 2.7 Archive gate and cause codes -> HANDS TO DOJO D6

- `ms/job/gate.py` (412): outcomes `correct_archive`, `wrong_archive` (below the bar or wrong class), `review`,
  `unscorable_archive` (fewer than `min_labelled` labelled fields), `crashed`; `_status` 202-235, `doc_status`
  238-248, `labelled_count` 163-179 (the sandbox scorable-field count the design names for the label layer, D4),
  `Bars` 96-105 / `load_bars` 121-134 (`config/production/bars.yaml`), `_wilson_upper` 182-188,
  `gate_report` 338-412 (pooled rates, per-class bars from `per_class_from_n`, never passes blind).
- `ms/job/gate_causes.py` (258) `primary_cause` 216-258. Cause tokens, by precedence: `mechanism:judge_error`,
  `mechanism:crashed`, `mechanism:transient`, `input:near_empty` (reviews only, under 200 characters),
  `classification:wrong_class | reviewer_unknown | reviewer_disagreement | arbiter_escalated`,
  `judge:partial_exhausted | judge_rejected`, `extraction:<field type> | cuad_missed | cuad_false | maud_wrong`,
  `label_suspect`, `mechanism:scorer_error`, `mechanism:unmapped:<raw cause>`. Reads the pipeline's
  `review_causes` tokens (vendored `pipeline/reconsideration.py`), so the record must store those tokens.
- `gate_audit.py` (141) audit-trail check (spans + node log + archive dir per document) -> Dojo log health on
  record verify; `gate_render.py` (180) Markdown; `gate_compare.py` (154) step-up verdict -> D7.
- Utility per outcome: `ms/autoresearch/scoring.py:doc_utility` 71-79 (2.6).

### 2.8 Run metrics, cards, serving parity, schema adherence, routing audit

| Capability | Location | Dojo chunk |
|---|---|---|
| Run metrics / serving record | `ms/job/metrics.py` (2,119) `record_from_run` 570-729, `enrich_serving_report` 825-949, `serving_record_from_store` 972-1062, `compare` 1176-1259, `per_document_rates` 1497-1547; projections `extrapolate_cost` 1550-1663, `estimate_suite` 1748-1984 | D5; projections D7 |
| Review rate | `ms/eval/human_review.py` (100) `summarize` 27-45 | D5 |
| Specialist and grid cards | `ms/job/grid_cards.py` (1,304) `token_split` 317-368, `collect_card` 415-621, `render_card_md` 832-892, `chunk_gate` 999-1036, `collect_suite` 1162-1189; `ms/job/grid_master.py` (1,255) `collect_master` 141-164, `render_master_md` 953-1043; study copies `sand42_cards.py` (1,611, retire) | D5 |
| Serving parity | `ms/eval/serving_parity.py` (470) `to_dojo_serving_record` 119-227, `score_cost_compare` 305-393, `records_from_run_items` 396-426; `ms/serving_groups.py` (206) | D5 (with the old Dojo `analysis/serving` core) |
| Schema adherence | `ms/eval/schema_adherence.py` (410) `schema_keys_for` 148-175, `assess_extraction_payload` 312-362, `merge_schema_adherence` 365-392, `classify_floor_mode` 395-410 | D5 |
| Routing audit | `ms/job/routing_audit.py` (260) `effective_thresholds` 47-61, `_escalation_cost` 123-139, `suggest_judge_band_high` 142-157, `_class_report` 160-196, `audit` 226-260 | D6 (behaviour) |
| Node totals | `ms/job/node_trace.py` `summarize` 265-278, `summarize_totals` 281-309 | D5 |

### 2.9 Cost truth, calibration, comparisons (other Dojo-bound code found)

`api-evals/api_evals/cost.py` (188, real OpenRouter billed cost) -> D7 cost truth; `scripts/calibrate_reviewer.py`
(361) and `scripts/extraction_gate.py` (599) -> D7 threshold calibration (Set A/B offline), output a policy file;
`scripts/compare_sorter_prompt.py` (461), `compare_specialist_prompt.py` (89), `ladder_pair_report.py` (37),
`ms/job/pair_report.py` (196) -> D7 paired comparison; `reports/dashboard/breakeven.py` (156) and
`deploy/run_gpu_cost.py` (1,366, split) -> D7 projections.

### 2.10 `tui/floor_replay.py:build_timeline` (the R7 recorded-replay contract) -> RECORD (other agent)

`ms/tui/floor_replay.py` (466), `build_timeline(source, *, lock=None)` 210-335; `_validate_source` 173-207;
callers `prepare_recorded_replay` 390-466 and `ms/tui/floor_app.py`.

- **Input:** a run folder. Reads `spec.lock.json` (unless `lock` is given), `checkpoint.json`, `nodes.jsonl`,
  `items.jsonl` (also size-checks `prompt.lock.json`, `dataset.jsonl`). Needs at least one of `nodes.jsonl` /
  `items.jsonl`. Refuses symlinks and non-regular files; limits 100 MB per file, 250 MB total, 200,000 rows,
  7 days of timeline. Malformed JSONL rows are skipped and counted in `warnings`.
- **Node row** (written by `ms/job/node_trace.py:NodeTracker._emit`): `doc`, `node`, `status` (`running`, `done`,
  `paused`, `error`), `agent`, `model`, `ts` (epoch seconds), plus `step` on running, `ms`, `stage`, `decisions` on
  done, `ms` on paused, `ms`, `error` on error. **Item row:** `item_id` (or `id`), `doc`, optional `ts`, and the
  rest copied verbatim (`ok`, `error`, `pred`, tokens, `usage_by_agent`). **Checkpoint:** `state`, `cursor`, `total`.
- **Algorithm:** `t0` = earliest parseable `ts`; with none the timeline is document-order and `synthetic`.
  Node starts/finishes pair by `(doc, node, step)`; an untimed finish lands at start + `ms`. Untimed items follow
  their document's last finished node, else the running cursor + 0.5 s.
- **Output:** `(events, meta)`. `events` is a list of `(at_seconds rounded to 4 dp, event)` sorted by time then
  sequence, where `event` is one of `{"checkpoint": {"state", "cursor", "total"}}`, `{"node": <row copy>}`,
  `{"item": <row copy>}`; the first is `checkpoint` state `running` cursor 0 at 0.0, the last a terminal
  `checkpoint` at end + 0.5 s whose state maps `cancelled` -> `failed`, keeps `prepared|running|paused|done|failed`,
  and a missing state -> `paused`. `meta`: `source_run_id`, `engine_kind`, `engine_model`, `profile`,
  `node_records`, `item_records`, `duration_s`, `synthetic_timing`, `incomplete` (state not done/failed),
  `source_state`, `warnings`.
- For the record: R7 lists node statuses running, done, paused, **failed**; the sandbox writes **`error`** for a
  failed node. The record's replay must say which it emits (finding 2).

### 2.11 Run folder layout the record importer reads

`ms/job/checkpoint.py` (680) `RunStore` 113-592 (paths 117-131), under `data/runtime/runs/<run_id>/`:

| File | Content |
|---|---|
| `spec.lock.json` | the locked run spec + resolved provenance (engine, profile, run id, spec hash, generation) |
| `prompt.lock.json` | resolved prompts and their hashes |
| `dataset.jsonl` | the locked draw, one row per document (`write_dataset` 294, `dataset_rows` 298) |
| `items.jsonl` | one row per processed document (`append_item` 343; torn-tail repair) |
| `nodes.jsonl` | node rows (2.10; `append_node` 536) |
| `events.jsonl` | `{"ts", "event", "level", "detail"}` (`append_event` 458; adds `events_truncated` on repair) |
| `checkpoint.json` | `schema_version`, `run_id`, `spec_hash`, `state` (`prepared`, `running`, `paused`, `failed`, `done`), `cursor`, `total`, `updated_at`, `remote`, `last_error` (`write_checkpoint` 412-457) |
| `cold_boot.json` | engine cold-boot probe |
| `spend-authorization.json` | the initials receipt (2.3) |
| `run.lock`, `cancel.lock`, `cancel.request.json`, `cancel.ack.json` | writer/control locks and cancel handshake |

`truncate_torn_tail` 64-96 and `generation_identity` 99-110 define how a torn or re-generated folder looks; the
importer should treat them the same way. Remote runs are pulled back into this layout (`remote.pull_run_dir`).

## 3. mailroom-reloaded: the TUI base

mailroom-reloaded is a whole second Mailroom (a CrewAI-Flows pipeline with its own vendored Dojo v0.21.0 subset,
span store, ledger and API). Only its **TUI enhancements** are the R7 base. The TUI is a browser terminal served at
`/tui` (static ES modules, no build step, no npm dependency) plus the `/ui` runs console, both fed by `/v1` routes in
`api/app.py`, with the `replay/v1` timeline built from a span store (`obs/replay/timeline.py`,
`schemas/replay.py`). Everything else retires or is an owner decision.

### 3.1 Browser TUI files (the part the launcher keeps)

| File (under `src/mailroom_reloaded/api/tui/`) | Lines | Role | Launcher target | Reads today -> reads in the launcher |
|---|---:|---|---|---|
| `ambient.js` | 179 | CRT overlay, skyline, sparks, reduced motion | `packages/launcher/src/mailroom_launcher/tui/web/ambient.js` | - |
| `api.js` | 202 | `/v1` client (uploads, replay, longer timeout) | `packages/launcher/src/mailroom_launcher/tui/web/api.js` | - |
| `banner-compact.txt` | 3 | banner | `packages/launcher/src/mailroom_launcher/tui/web/banner-compact.txt` | - |
| `banner.txt` | 6 | banner | `packages/launcher/src/mailroom_launcher/tui/web/banner.txt` | - |
| `boot.js` | 156 | boot checks (`/health`, auth probe, catalog, eval runs) | `packages/launcher/src/mailroom_launcher/tui/web/boot.js` | `/health`, `/v1/documents`, `/runs` -> launcher server `/health`, record index |
| `commands/ledger.js` | 148 | `ledger` list / head / verify | `packages/launcher/src/mailroom_launcher/tui/web/commands/ledger.js` | `/v1/ledger*` -> record verify and trace |
| `commands/pipeline.js` | 682 | `ls`, `inspect`, `audit`, `jev`, `review`, `resolve`, `runs`, `cards`, `health`, `upload`, `watch`, `auth` | `packages/launcher/src/mailroom_launcher/tui/web/commands/pipeline.js` | `/v1/documents`, `/audit`, `/jev`, `/review`, `/runs`, `/runs/{id}/cards` -> record index (documents, runs), record verify (audit), Dojo cards; `upload` -> engine API; `review`/`resolve` -> review station; `jev` -> policy decision-slot view |
| `commands/replay.js` | 536 | `replay` sessions and viewer driver | `packages/launcher/src/mailroom_launcher/tui/web/commands/replay.js` | `/v1/replay/sessions*` -> record replay timeline via a `replay/v1` adapter |
| `commands/shell.js` | 175 | `help`, `man`, `clear`, `history`, `neofetch`, `theme` (+`crt`, `skyline`) | `packages/launcher/src/mailroom_launcher/tui/web/commands/shell.js` | - |
| `deeplink.js` | 29 | `#replay=run:<id>` deep links | `packages/launcher/src/mailroom_launcher/tui/web/deeplink.js` | - |
| `engine.js` | 203 | command registry, parsing, completion | `packages/launcher/src/mailroom_launcher/tui/web/engine.js` | - |
| `index.html` | 30 | `/tui` page | `packages/launcher/src/mailroom_launcher/tui/web/index.html` | - |
| `lib/fmt.js` | 45 | bytes, duration, usd | `packages/launcher/src/mailroom_launcher/tui/web/lib/fmt.js` | - |
| `lib/http.js` | 126 | JSON client, typed errors | `packages/launcher/src/mailroom_launcher/tui/web/lib/http.js` | - |
| `lib/links.js` | 34 | `GET /links` helpers | `packages/launcher/src/mailroom_launcher/tui/web/lib/links.js` | - |
| `lib/poll.js` | 97 | non-overlapping poller | `packages/launcher/src/mailroom_launcher/tui/web/lib/poll.js` | - |
| `lib/table.js` | 69 | aligned text tables | `packages/launcher/src/mailroom_launcher/tui/web/lib/table.js` | - |
| `main.js` | 115 | boot order, MODULES list (`pipeline`, `ledger`, `replay`, `shell`), registerAll | `packages/launcher/src/mailroom_launcher/tui/web/main.js` | - |
| `plugins.js` | 159 | `createModuleHost`: registerModule, all-or-nothing, sanitised panels, isolated onInit | `packages/launcher/src/mailroom_launcher/tui/web/plugins.js` | - |
| `replay/clock.js` | 117 | play/seek/speed clock | `packages/launcher/src/mailroom_launcher/tui/web/replay/clock.js` | - |
| `replay/grid.js` | 468 | character-grid renderer, sanitizeRows | `packages/launcher/src/mailroom_launcher/tui/web/replay/grid.js` | - |
| `replay/live.js` | 141 | SSE follow of `/v1/replay/live` | `packages/launcher/src/mailroom_launcher/tui/web/replay/live.js` | `/v1/replay/live` SSE over spans -> SSE over the record event tail |
| `replay/model.js` | 377 | timeline model at a playhead | `packages/launcher/src/mailroom_launcher/tui/web/replay/model.js` | - |
| `replay/panels.js` | 392 | insight panels + `registerPanel` | `packages/launcher/src/mailroom_launcher/tui/web/replay/panels.js` | - |
| `replay/stations.js` | 17 | station track | `packages/launcher/src/mailroom_launcher/tui/web/replay/stations.js` | - |
| `terminal.js` | 632 | terminal surface: scrollback, input, history, keys | `packages/launcher/src/mailroom_launcher/tui/web/terminal.js` | - |
| `tokens.css` | 100 | brand palette tokens (shared with `/ui`) | `packages/launcher/src/mailroom_launcher/tui/web/tokens.css` | - |
| `tui.css` | 476 | terminal layout | `packages/launcher/src/mailroom_launcher/tui/web/tui.css` | - |
| `../ui/app.js` | 77 | `/ui` logic | `packages/launcher/src/mailroom_launcher/tui/web/ui/app.js` | - |
| `../ui/index.html` | 324 | `/ui` runs console | `packages/launcher/src/mailroom_launcher/tui/web/ui/index.html` | - |
| `../ui/ui.css` | 223 | `/ui` layout | `packages/launcher/src/mailroom_launcher/tui/web/ui/ui.css` | - |
| **total** | 6,338 | | | |

### 3.2 Python modules (all of them, for completeness)


#### `deploy` (3 modules, 288 lines)

| Module | Lines | What it does | Verdict | Target | Depends on / must cut |
|---|---:|---|---|---|---|
| `mock_jev.py` | 76 | Dev-only Jev (TypeSafe System One) stand-in: ``POST /v1/systemone``. *Jev dev stand-in* | OPEN | - | - |
| `mock_openai.py` | 67 | Dev-only OpenAI endpoint: fixed synthetic correspondence, no inference. *fixed synthetic responses, no inference* | LAUNCHER | `packages/launcher/tests/fakes/` (L2 mock backend) | - |
| `modal_vllm.py` | 145 | Modal-deployed vLLM for mailroom-reloaded (OpenAI-compatible, with /metrics). *the sandbox `deploy/modal_vllm.py` (837 lines) is the source* | RETIRE | - | - |

#### `scripts` (7 modules, 1,057 lines)

| Module | Lines | What it does | Verdict | Target | Depends on / must cut |
|---|---:|---|---|---|---|
| `demo_release_notes.py` | 101 | Print Markdown that embeds every demo screenshot, for PR bodies and GitHub release notes. | RETIRE | - | - |
| `demo_seed_eval_runs.py` | 47 | Seed two synthetic ``eval_docs`` rows so the /ui "Eval runs" table lists a run. | LAUNCHER | `packages/launcher/scripts/` (L3 dev harness, rebuilt over a synthetic record) | - |
| `dev_env.py` | 47 | Read dev helper settings as dotenv data, without shell or $ interpolation. | LAUNCHER | `packages/launcher/scripts/` (L3 dev harness, rebuilt over a synthetic record) | - |
| `fetch_dataset_sample.py` | 116 | Pull one document from ``Lucius-Morningstar/mailroom-dataset`` and optionally upload it. | RETIRE | - | - |
| `jev_dev_rows.py` | 25 | Deterministic synthetic Jev calibration rows (split=train) for the dev stack. *Jev* | OPEN | - | - |
| `jev_export_gate_features.py` | 259 | Export evaluator gate-feature rows + ground-truth labels for Jev harvesting. *Jev* | OPEN | - | - |
| `jev_harvest.py` | 462 | Harvest Jev answers over the real mailroom TRAIN distribution (issue #8). *Jev* | OPEN | - | - |

#### `scripts/tui_seed_replay` (1 modules, 106 lines)

| Module | Lines | What it does | Verdict | Target | Depends on / must cut |
|---|---:|---|---|---|---|
| `seed_replay.py` | 106 | Seed one replayable eval run into the dev span store (``scripts/tui_dev.sh``). *seed one replayable run: becomes a synthetic record* | LAUNCHER | `packages/launcher/scripts/seed_replay.py` (L3/L4) | - |

#### `src/mailroom_reloaded` (6 modules, 2,115 lines)

| Module | Lines | What it does | Verdict | Target | Depends on / must cut |
|---|---:|---|---|---|---|
| `__init__.py` | 11 | mailroom-reloaded: a compressed Digital Mailroom on CrewAI Flows. *CrewAI pipeline: a second engine; the engine is LangGraph `legacy` (design). Ideas (2-call path, deterministic gate) can return as a policy* | RETIRE | - | - |
| `cli.py` | 772 | ``mailroom`` command line (plan Task 19; console script ``mailroom``). *typer CLI* | SPLIT | `serve`/`watch`/`run` -> engine; `eval` -> launcher run start; `card` -> Dojo; `audit verify` -> record verify; `replay import/export/sessions` -> record + launcher; `jev`, `gmail`, `pager` OPEN | - |
| `review.py` | 253 | Human-review resolution (spec section 4). | REVIEW STATION | other agent | - |
| `settings.py` | 440 | Runtime settings and the taxonomy contract. *CrewAI pipeline: a second engine; the engine is LangGraph `legacy` (design). Ideas (2-call path, deterministic gate) can return as a policy* | RETIRE | - | - |
| `tools.py` | 308 | One tool definition, two surfaces: CrewAI ``BaseTool`` and OpenAI function spec. *CrewAI pipeline: a second engine; the engine is LangGraph `legacy` (design). Ideas (2-call path, deterministic gate) can return as a policy* | RETIRE | - | - |
| `watcher.py` | 331 | Filesystem watcher: drain ``inbox/`` into the pipeline, one claim per file. *CrewAI pipeline: a second engine; the engine is LangGraph `legacy` (design). Ideas (2-call path, deterministic gate) can return as a policy* | RETIRE | - | - |

#### `src/mailroom_reloaded/agents` (8 modules, 1,562 lines)

| Module | Lines | What it does | Verdict | Target | Depends on / must cut |
|---|---:|---|---|---|---|
| `__init__.py` | 1 | Agent runtimes: sorter, specialists, route gate, judge, arbiter and boss. *CrewAI pipeline: a second engine; the engine is LangGraph `legacy` (design). Ideas (2-call path, deterministic gate) can return as a policy* | RETIRE | - | - |
| `arbiter.py` | 72 | CrewAI arbiter: settles judge/specialist disagreements with a bounded action. *CrewAI pipeline: a second engine; the engine is LangGraph `legacy` (design). Ideas (2-call path, deterministic gate) can return as a policy* | RETIRE | - | - |
| `boss.py` | 65 | CrewAI boss: adjudicates escalation, reassigning the class or parking for a human. *CrewAI pipeline: a second engine; the engine is LangGraph `legacy` (design). Ideas (2-call path, deterministic gate) can return as a policy* | RETIRE | - | - |
| `gate.py` | 215 | Deterministic route gate (replaces the reviewer nodes). Zero LLM calls. *Jev / learned route gate; owner decision* | OPEN | decision-slot experiment (R10) if wanted | - |
| `jev.py` | 418 | Jev (TypeSafe System One) learned route gate (issue #8). *Jev / learned route gate; owner decision* | OPEN | decision-slot experiment (R10) if wanted | - |
| `judge.py` | 188 | CrewAI judge: live completeness verification and eval ground-truth grading. *CrewAI pipeline: a second engine; the engine is LangGraph `legacy` (design). Ideas (2-call path, deterministic gate) can return as a policy* | RETIRE | - | - |
| `sorter.py` | 222 | Sorter (spec sections 5 and 6): the FULL / SUBCLASS_ONLY standalone call. *CrewAI pipeline: a second engine; the engine is LangGraph `legacy` (design). Ideas (2-call path, deterministic gate) can return as a policy* | RETIRE | - | - |
| `specialists.py` | 381 | Frozen-v1 specialists (spec section 7): one structured call per class. *CrewAI pipeline: a second engine; the engine is LangGraph `legacy` (design). Ideas (2-call path, deterministic gate) can return as a policy* | RETIRE | - | - |

#### `src/mailroom_reloaded/api` (2 modules, 1,200 lines)

| Module | Lines | What it does | Verdict | Target | Depends on / must cut |
|---|---:|---|---|---|---|
| `__init__.py` | 5 | FastAPI ``/v1`` API and the localhost runs UI (spec sections 3 and 9). *L4* | LAUNCHER | `packages/launcher/src/mailroom_launcher/server/__init__.py` | - |
| `app.py` | 1,195 | The mailroom HTTP API: ``/v1`` endpoints, ``/health`` and the ``/ui`` page. *1,195 lines* | SPLIT | read routes for `/tui`, `/ui` (documents, runs, cards, replay, ledger, links, health) -> launcher L4 server; upload/intake -> engine E6; review resolve -> review station | - |

#### `src/mailroom_reloaded/api/routes` (2 modules, 310 lines)

| Module | Lines | What it does | Verdict | Target | Depends on / must cut |
|---|---:|---|---|---|---|
| `__init__.py` | 5 | Routers for the Phase 6 operator routes; ``app.py`` includes them and keeps its own routes. *L4* | LAUNCHER | `packages/launcher/src/mailroom_launcher/server/routes/` | - |
| `ready.py` | 305 | ``GET /ready``: aggregate readiness of the app's backends (Phase 6 P6-A, item A4). *L4 readiness probes (components change: record index, backends)* | LAUNCHER | `packages/launcher/src/mailroom_launcher/server/routes/ready.py` | - |

#### `src/mailroom_reloaded/eval` (10 modules, 4,104 lines)

| Module | Lines | What it does | Verdict | Target | Depends on / must cut |
|---|---:|---|---|---|---|
| `__init__.py` | 45 | Evaluation: blind/ground-truth dataset loader and the eval runner (Task 20). *SAND-37-parity copy of sandbox cards/runner; sandbox sources go to Dojo D5 / launcher L7 instead* | RETIRE | - | - |
| `cards.py` | 670 | SAND-37-parity score & cost cards (Task 21, spec section 8). *SAND-37-parity copy of sandbox cards/runner; sandbox sources go to Dojo D5 / launcher L7 instead* | RETIRE | - | - |
| `conformance.py` | 725 | Behavioural conformance suite for every LLM role (spec section 11, Task 24). *behavioural conformance per LLM role* | OPEN | engine test idea (E2) | - |
| `cost.py` | 210 | Cell cost (GPU-hour and per-token) and prompt-token composition (Task 21). *SAND-37-parity copy of sandbox cards/runner; sandbox sources go to Dojo D5 / launcher L7 instead* | RETIRE | - | - |
| `dataset.py` | 542 | Blind/ground-truth dataset loader and nested sampler (spec section 8, Task 20). *SAND-37-parity copy of sandbox cards/runner; sandbox sources go to Dojo D5 / launcher L7 instead* | RETIRE | - | - |
| `jev_calibration.py` | 201 | Fit the Jev (TypeSafe System One) confidence calibration (issue #8). *Jev / learned route gate; owner decision* | OPEN | decision-slot experiment (R10) if wanted | - |
| `metrics.py` | 531 | Sorter, specialist and gate KPIs for the SAND-37 cards (Task 21). *SAND-37-parity copy of sandbox cards/runner; sandbox sources go to Dojo D5 / launcher L7 instead* | RETIRE | - | - |
| `runner.py` | 764 | Evaluation runner (spec section 8, Task 20). *SAND-37-parity copy of sandbox cards/runner; sandbox sources go to Dojo D5 / launcher L7 instead* | RETIRE | - | - |
| `train_gate.py` | 268 | Fit the route gate's calibration and learned logistic models (Task 13). *Jev / learned route gate; owner decision* | OPEN | decision-slot experiment (R10) if wanted | - |
| `vllm_telemetry.py` | 148 | vLLM Prometheus telemetry: scrape, parse and delta (Task 21, spec section 8). *sandbox `vllm_metrics` is the source* | RETIRE | - | - |

#### `src/mailroom_reloaded/ingest` (5 modules, 552 lines)

| Module | Lines | What it does | Verdict | Target | Depends on / must cut |
|---|---:|---|---|---|---|
| `__init__.py` | 0 |  *CrewAI pipeline: a second engine; the engine is LangGraph `legacy` (design). Ideas (2-call path, deterministic gate) can return as a policy* | RETIRE | - | - |
| `bert.py` | 165 | ModernBERT primary classifier adapter and sorter handoff policy (spec section 5). *CrewAI pipeline: a second engine; the engine is LangGraph `legacy` (design). Ideas (2-call path, deterministic gate) can return as a policy* | RETIRE | - | - |
| `clerk.py` | 225 | Deterministic ingest: read a file, extract text, run the intake clerk. *CrewAI pipeline: a second engine; the engine is LangGraph `legacy` (design). Ideas (2-call path, deterministic gate) can return as a policy* | RETIRE | - | - |
| `pdf.py` | 64 | PDF text-layer extraction: pypdf, then pdfplumber, then pymupdf. *CrewAI pipeline: a second engine; the engine is LangGraph `legacy` (design). Ideas (2-call path, deterministic gate) can return as a policy* | RETIRE | - | - |
| `vision.py` | 98 | Vision fallback: render PDF pages to images and transcribe them with the vision model. *CrewAI pipeline: a second engine; the engine is LangGraph `legacy` (design). Ideas (2-call path, deterministic gate) can return as a policy* | RETIRE | - | - |

#### `src/mailroom_reloaded/intake` (2 modules, 552 lines)

| Module | Lines | What it does | Verdict | Target | Depends on / must cut |
|---|---:|---|---|---|---|
| `__init__.py` | 7 | Intake adapters: alternate ways documents enter the mailroom inbox. *Gmail intake (same open item as llm-mailroom)* | OPEN | - | - |
| `gmail.py` | 545 | Gmail attachment intake: list supported attachments and queue them in ``inbox/``. *Gmail intake (owner decision)* | OPEN | - | - |

#### `src/mailroom_reloaded/llm` (6 modules, 1,160 lines)

| Module | Lines | What it does | Verdict | Target | Depends on / must cut |
|---|---:|---|---|---|---|
| `__init__.py` | 0 |  *CrewAI pipeline: a second engine; the engine is LangGraph `legacy` (design). Ideas (2-call path, deterministic gate) can return as a policy* | RETIRE | - | - |
| `client.py` | 462 | OpenAI-compatible client: provider resolution, CrewAI LLM factory, structured calls. *CrewAI pipeline: a second engine; the engine is LangGraph `legacy` (design). Ideas (2-call path, deterministic gate) can return as a policy* | RETIRE | - | - |
| `health.py` | 185 | Passive per-provider LLM health for the operator pager (port of llm-mailroom's `llm/provider_health.py`). *pager provider health (with the pager)* | OPEN | - | - |
| `retry.py` | 149 | Transient-failure retry for LLM calls (ported from llm-mailroom, minus gateway and free swarm). *CrewAI pipeline: a second engine; the engine is LangGraph `legacy` (design). Ideas (2-call path, deterministic gate) can return as a policy* | RETIRE | - | - |
| `tooling.py` | 288 | Standalone tool loop (spec section 7): bounded tool rounds, then a forced final answer. *CrewAI pipeline: a second engine; the engine is LangGraph `legacy` (design). Ideas (2-call path, deterministic gate) can return as a policy* | RETIRE | - | - |
| `usage.py` | 76 | Token and latency accounting for LLM calls. *CrewAI pipeline: a second engine; the engine is LangGraph `legacy` (design). Ideas (2-call path, deterministic gate) can return as a policy* | RETIRE | - | - |

#### `src/mailroom_reloaded/obs` (10 modules, 3,379 lines)

| Module | Lines | What it does | Verdict | Target | Depends on / must cut |
|---|---:|---|---|---|---|
| `__init__.py` | 6 | Observability: OpenTelemetry traces (OpenInference) and pipeline metrics. *OTel/OpenInference span pipeline; the record replaces spans as source of truth* | RETIRE | - | - |
| `attrs.py` | 215 | Attribute vocabulary shared by spans, the ledger and the replay builder. *OTel/OpenInference span pipeline; the record replaces spans as source of truth* | RETIRE | - | - |
| `metrics.py` | 224 | OpenTelemetry metrics (spec section 8, "Live OTel metrics"). *OTel/OpenInference span pipeline; the record replaces spans as source of truth* | RETIRE | - | - |
| `pager.py` | 1,018 | Operator SMS pager host: ledger-backed snapshot, scheduler, settings, CLI. *SMS pager (same open item as llm-mailroom `pager*`)* | OPEN | - | - |
| `pager_core.py` | 834 | Operator SMS pager core (shared verbatim with llm-mailroom/src/pipeline/pager_core.py). *SMS pager (same open item as llm-mailroom `pager*`)* | OPEN | - | - |
| `reconsideration.py` | 148 | Objective review causes (port of The-Mailroom ``reconsideration.collect_review_causes``). *OTel/OpenInference span pipeline; the record replaces spans as source of truth* | RETIRE | - | - |
| `run_context.py` | 130 | Run scope: which run a unit of work belongs to (metric labels, ledger, spans). *OTel/OpenInference span pipeline; the record replaces spans as source of truth* | RETIRE | - | - |
| `scores.py` | 257 | Score registry and emission: scores live on the span they describe. *OTel/OpenInference span pipeline; the record replaces spans as source of truth* | RETIRE | - | - |
| `sms.py` | 247 | Operator SMS pager transports: Twilio, carrier email gateway and console, with recipient validation, log masking and GSM-7/UCS-2 segment counting. Sen *SMS pager (same open item as llm-mailroom `pager*`)* | OPEN | - | - |
| `tracing.py` | 300 | OpenTelemetry tracing setup (spec section 2, section 9). *OTel/OpenInference span pipeline; the record replaces spans as source of truth* | RETIRE | - | - |

#### `src/mailroom_reloaded/obs/replay` (5 modules, 1,989 lines)

| Module | Lines | What it does | Verdict | Target | Depends on / must cut |
|---|---:|---|---|---|---|
| `__init__.py` | 11 | Replay timeline builders (``replay/v1``): spans first, audit log as the approximate fallback. | RECORD | record replay timeline (other agent) | - |
| `audit_source.py` | 509 | Approximate ``replay/v1`` timeline from the hash-chained audit log. *approximate timeline from an audit log; the record is exact* | RETIRE | - | - |
| `otlp_import.py` | 369 | Offline OTLP/JSON import into the span store (the ``mailroom replay import`` backend). *OTLP import into a span store; the record importer replaces it* | RETIRE | - | - |
| `sessions.py` | 308 | Replay session ids and the session picker. *session ids and picker* | LAUNCHER | `packages/launcher/src/mailroom_launcher/server/sessions.py` (L4) over the record index | - |
| `timeline.py` | 792 | Build a ``replay/v1`` :class:`Timeline` from stored span rows. *`replay/v1` payload the browser viewer reads; see section 3* | RECORD | record replay timeline (Task 10b) + launcher `replay/v1` view adapter | - |

#### `src/mailroom_reloaded/pipeline` (8 modules, 2,545 lines)

| Module | Lines | What it does | Verdict | Target | Depends on / must cut |
|---|---:|---|---|---|---|
| `__init__.py` | 21 | Pipeline package: flow state, guards, report, archivist and ``MailroomFlow``. *CrewAI pipeline: a second engine; the engine is LangGraph `legacy` (design). Ideas (2-call path, deterministic gate) can return as a policy* | RETIRE | - | - |
| `archivist.py` | 80 | Deterministic archivist (ported from ``agents/archivist.py``). *CrewAI pipeline: a second engine; the engine is LangGraph `legacy` (design). Ideas (2-call path, deterministic gate) can return as a policy* | RETIRE | - | - |
| `flow.py` | 1,114 | CrewAI ``MailroomFlow`` (spec section 4). *CrewAI pipeline: a second engine; the engine is LangGraph `legacy` (design). Ideas (2-call path, deterministic gate) can return as a policy* | RETIRE | - | - |
| `guards.py` | 73 | Node guards: manifest checkpointing, audit entries, spans and budgets. *CrewAI pipeline: a second engine; the engine is LangGraph `legacy` (design). Ideas (2-call path, deterministic gate) can return as a policy* | RETIRE | - | - |
| `report.py` | 132 | Deterministic report writer (ported from ``agents/reporter.py``). *CrewAI pipeline: a second engine; the engine is LangGraph `legacy` (design). Ideas (2-call path, deterministic gate) can return as a policy* | RETIRE | - | - |
| `run_ledger.py` | 495 | Ledger intake: turn finished documents and runs into archive-ledger entries. | RECORD | - | - |
| `state.py` | 79 | CrewAI flow state for the Digital Mailroom pipeline (spec section 4). *CrewAI pipeline: a second engine; the engine is LangGraph `legacy` (design). Ideas (2-call path, deterministic gate) can return as a policy* | RETIRE | - | - |
| `trace_capture.py` | 551 | Span attributes, decision events and scores for the pipeline (replay capture). *span capture; the engine writes events* | RETIRE | - | - |

#### `src/mailroom_reloaded/prompts` (2 modules, 81 lines)

| Module | Lines | What it does | Verdict | Target | Depends on / must cut |
|---|---:|---|---|---|---|
| `__init__.py` | 0 |  *CrewAI pipeline: a second engine; the engine is LangGraph `legacy` (design). Ideas (2-call path, deterministic gate) can return as a policy* | RETIRE | - | - |
| `loader.py` | 81 | Load packaged prompts; frozen sets are sha256-locked to their lineage.json. *CrewAI pipeline: a second engine; the engine is LangGraph `legacy` (design). Ideas (2-call path, deterministic gate) can return as a policy* | RETIRE | - | - |

#### `src/mailroom_reloaded/schemas` (6 modules, 920 lines)

| Module | Lines | What it does | Verdict | Target | Depends on / must cut |
|---|---:|---|---|---|---|
| `__init__.py` | 0 |  *CrewAI pipeline: a second engine; the engine is LangGraph `legacy` (design). Ideas (2-call path, deterministic gate) can return as a policy* | RETIRE | - | - |
| `audit.py` | 50 | Audit log entry schema and hash-chain helpers. | RECORD | - | - |
| `extraction.py` | 355 | Per-class extraction schemas, strict JSON schema builder and compliance checks. *CrewAI pipeline: a second engine; the engine is LangGraph `legacy` (design). Ideas (2-call path, deterministic gate) can return as a policy* | RETIRE | - | - |
| `ledger.py` | 265 | Archive-ledger entry schema, payload allow-list and hash helpers. *archive-ledger entry schema* | RECORD | - | - |
| `manifest.py` | 34 |  *CrewAI pipeline: a second engine; the engine is LangGraph `legacy` (design). Ideas (2-call path, deterministic gate) can return as a policy* | RETIRE | - | - |
| `replay.py` | 216 | ``replay/v1`` timeline models. *`replay/v1` view models; built from the record timeline* | LAUNCHER | `packages/launcher/src/mailroom_launcher/server/replay_view.py` (L4) | - |

#### `src/mailroom_reloaded/scoring` (12 modules, 4,820 lines)

| Module | Lines | What it does | Verdict | Target | Depends on / must cut |
|---|---:|---|---|---|---|
| `__init__.py` | 52 | Slim vendored subset of ``llm-dojo-scoring`` v0.21.0 (see PARITY.md). *vendored Dojo v0.21.0 subset; the workspace Dojo owns scoring* | RETIRE | - | - |
| `classification.py` | 312 | Classification scorers: normalization, exact match, per-class stats, confusion matrices, and binary metrics. *vendored Dojo v0.21.0 subset; the workspace Dojo owns scoring* | RETIRE | - | - |
| `config.py` | 628 | Central, importable configuration for the dojo scoring suite. *vendored Dojo v0.21.0 subset; the workspace Dojo owns scoring* | RETIRE | - | - |
| `corpus.py` | 539 | mailroom-dataset corpus alignment — schemas, subclasses, differentiators. *vendored Dojo v0.21.0 subset; the workspace Dojo owns scoring* | RETIRE | - | - |
| `equivalences.py` | 76 | Subtype / doc-subclass normalization and equivalence helpers. *vendored Dojo v0.21.0 subset; the workspace Dojo owns scoring* | RETIRE | - | - |
| `extraction_metrics.py` | 217 | Field-micro precision / recall / F1 / F2 over extraction events. *vendored Dojo v0.21.0 subset; the workspace Dojo owns scoring* | RETIRE | - | - |
| `field_scoring.py` | 1,358 | Deterministic, field-type-aware extraction scoring. *vendored Dojo v0.21.0 subset; the workspace Dojo owns scoring* | RETIRE | - | - |
| `gt_metadata.py` | 298 | Parse mailroom-dataset ground-truth metadata into scoring-ready fields. *vendored Dojo v0.21.0 subset; the workspace Dojo owns scoring* | RETIRE | - | - |
| `intake.py` | 399 | Intake clerk — pre-sorter text cleaning, format fixes, and prep. *vendored Dojo v0.21.0 subset; the workspace Dojo owns scoring* | RETIRE | - | - |
| `intents.py` | 196 | Controlled ``intent`` vocabularies for the three classes that carry one. *vendored Dojo v0.21.0 subset; the workspace Dojo owns scoring* | RETIRE | - | - |
| `maud.py` | 506 | MAUD merger-agreement answer-class catalogs — no guessing on GT labels. *vendored Dojo v0.21.0 subset; the workspace Dojo owns scoring* | RETIRE | - | - |
| `trace_knobs.py` | 239 | Confidence and reasoning as captured experimental knobs. *vendored Dojo v0.21.0 subset; the workspace Dojo owns scoring* | RETIRE | - | - |

#### `src/mailroom_reloaded/storage` (9 modules, 2,730 lines)

| Module | Lines | What it does | Verdict | Target | Depends on / must cut |
|---|---:|---|---|---|---|
| `__init__.py` | 0 |  *CrewAI pipeline: a second engine; the engine is LangGraph `legacy` (design). Ideas (2-call path, deterministic gate) can return as a policy* | RETIRE | - | - |
| `anchor.py` | 703 | External anchor for the archive ledger head. *other agent decides* | OPEN | record (external anchor of a chain head) | - |
| `audit_log.py` | 141 | Append-only, hash-chained audit log. | RECORD | - | - |
| `bins.py` | 151 |  *CrewAI pipeline: a second engine; the engine is LangGraph `legacy` (design). Ideas (2-call path, deterministic gate) can return as a policy* | RETIRE | - | - |
| `catalog.py` | 84 | Document catalog (one row per doc_id). *CrewAI pipeline: a second engine; the engine is LangGraph `legacy` (design). Ideas (2-call path, deterministic gate) can return as a policy* | RETIRE | - | - |
| `db.py` | 116 | SQLite engine, schema and the lazily created default engine. *CrewAI pipeline: a second engine; the engine is LangGraph `legacy` (design). Ideas (2-call path, deterministic gate) can return as a policy* | RETIRE | - | - |
| `ledger.py` | 753 | Archive ledger: one global hash chain over every pipeline run. *one global chain; the record uses per-document chains* | RECORD | - | - |
| `retention.py` | 339 | Span retention: keep classes, pin/unpin, prune and the showcase seed. | RECORD | record pack/prune (Task 11); TUI `runs pin/unpin/keep` reads it | - |
| `span_store.py` | 443 | Durable local span store: the replay viewer's source of truth. *span store = replay source; replaced by the record* | RECORD | - | - |

### 3.3 Reloaded TUI features the launcher TUI must keep

From `docs/TUI.md` (359 lines) and the code above:

1. **Browser terminal at `/tui`**, static ES modules, no build step, no npm dependency; every printed value comes
   from a live API call, nothing canned; an unreachable API says so and prints no data.
2. **Boot checks** (`boot.js`): `/health`, auth probe, catalog count, eval-run count; a failing optional check
   prints `[ !! ]` without closing the terminal.
3. **Plugin boundary** (`plugins.js`): `registerModule({id, commands, panels, onInit})`, reserved ids, all-or-nothing
   command staging, sanitised panel rows, isolated `onInit` (a bad module never blocks boot). This is the seam for
   adding the sandbox features and for the review station's commands.
4. **Commands**: `help`, `man`, `clear`, `history` (token lines never kept), `neofetch`, `theme`, `crt`, `skyline`,
   `ls`, `inspect`, `audit`, `runs` (+ `pin`, `unpin`, `keep`, `keep set`), `ledger` (list, `head`, `verify`),
   `replay`, `cards`, `health`, `upload`, `watch`, `auth`; `jev`, `review`, `resolve` move out (OPEN, review station).
5. **Replay viewer**: character grid with station track and phase headers, scrub bar with event ticks and sector
   shading, inspector / ledger / insight panels (metrics, tokens, decisions, latency, fields, leaderboard, ticker,
   pluggable via `registerPanel`), keys (Space, arrows, Shift, `[` `]`, digits, `r`, `j`/`k`, `i`, `l`, `p`, `e`, `f`,
   `o`, `g`, `?`, `q`/Esc), per-document elapsed time, tokens and cost at the playhead; a pruned run answers 410.
6. **Live follow** (`replay/live.js`): SSE on `/v1/replay/live` with `ready`, item, `heartbeat`, `error` frames,
   server clock (`now - 2 s`), row-cap error frame (`row_cap`), bounded memory (2 h / 20,000 items), pause when hidden.
7. **Deep links** `#replay=run:<id>` (token never in the URL); `/ui` rows link to them.
8. **Themes** (`dark`, `light`, `hc`, phosphors `amber`, `green`, `cyan`), CRT and skyline toggles,
   `prefers-reduced-motion` honoured, choice in `localStorage`.
9. **Auth**: bearer token only in `sessionStorage`, masked in scrollback and history, `auth --clear`.
10. **Safety**: `textContent` only (no `innerHTML`), control/bidi characters replaced, every failure printed with its
    reason (`internal error:`, `viewer error:`), nothing fails silently.
11. **`/ui` runs console**: documents with status filter, review queue, eval runs with replay links, detail pane,
    audit-verify box; shares `tokens.css`.
12. **`GET /ready`** (`api/routes/ready.py`, 305): concurrent probes with a 2 s budget, `ok | degraded | down |
    unconfigured`, 503 only when the core store is down; components become record index, backends, engine.
13. **Retention controls** (`runs pin/unpin/keep`) map onto record pack/prune (record Task 11).
14. **Tests**: 23 node test files under `tests/tui/js/` (`plugins.test.mjs`, `lib-*.test.mjs`, replay) plus
    Python `tests/tui/test_engine_js.py`, `test_replay_seed.py`, `test_replay_stations.py`; dev harness
    `scripts/tui_dev.sh`, `scripts/tui_seed_replay/seed_replay.py` (106) and a browser check
    `scripts/tui_replay_check.mjs`. CI needs Node for these (finding 8).

Dropped from the reloaded TUI: Phoenix/Grafana outbound links (`o`, `g`, `GET /links`) unless an exporter is kept;
`jev` (a decision-slot view could replace it, R10).

### 3.4 Native sandbox CLI and TUI features to add (R7 inventory)

**CLI** (`sandbox`, argparse, `ms/cli.py` `build_parser` 235-1151 plus groups registered from other modules; 92
subcommands in `cli.py` alone). Proposed fate per group:

| Group (sandbox command) | Subcommands | Fate |
|---|---|---|
| `run` | `preflight`, `start`, `status`, `routing`, `gate`, `resume`, `cancel`, `list`, `benchmark-check`, `deploy-env`, `scrape-metrics`, `card`, `export-bt`, `dispose`, `suite` | KEEP (L1, L6, L7); `routing`, `gate`, `card` call the Dojo; `export-bt` retires |
| `experiment` (`experiments.py` 987+) | `designs`, `create`, `review`, `preflight`, `launch`, `watch`, `report` | KEEP (L8); `report` calls the Dojo |
| `autoresearch` | `plan`, `step`, `run`, `status`, `holdout` | KEEP (L8) |
| `doctor` | - | KEEP (L2) |
| `modal` | `deploy`, `publish-image`, `prewarm`, `status`, `stop`, `logs`, `url`, `raw` | KEEP (L2) |
| `vllm` | `probe`, `command`, `serve`, `metrics` | KEEP (L2) |
| `hf` | `whoami`, `download`, `cache`, `raw` | KEEP read-only (L1); never upload |
| `datasets` | `pull`, `sample`, `prepare` | KEEP (L1) |
| `up`, `down`, `health`, `pull-models` | - | KEEP (L2 Compose/local) |
| `tunnel` | `plan`, `up`, `status`, `down` | KEEP (L2) |
| `profiles`, `modal-matrix` (`list`, `show`, `env`) | - | KEEP (L2, config readers) |
| `metrics` | `compare`, `extrapolate`, `serving-record`, `estimate-suite` | Dojo D5/D7 commands; launcher keeps only `estimate-suite` as a spend estimate |
| `scorecard` | - | Dojo report command |
| `eval` (`specialist`, `pipeline`), `pilot`, `hf-pilot`, `matrix` | - | KEEP as `run start` presets (L1/L7) |
| `watch`, `web`, `dev`, `board`, `floor`, `beacon update`, `tui` | - | KEEP (L3, L4) |
| `traces` | `export`, `pack` | RETIRE (record trace and pack replace them) |
| `runbook` | `list`, `show`, `check`, `write` | OPEN (keep the reader if runbooks stay) |
| `prompts` | `list`, `show`, `list-versions` | KEEP as a config reader (prompt sets) |
| `agents` (`list`, `show`), `cutover` | - | KEEP as views of the agent-to-model map |
| `review` | `list`, `show`, `resolve` | REVIEW STATION |
| `api` | `status`, `eval` | RETIRE (`family_api`) |
| `pipeline` | `watcher`, `api` | ENGINE (the engine's own CLI) |
| `subagents` (+ `preflight`, `dispatch-check`, `checkpoint`), `fetch-deps`, snapshots group, `modernbert` | - | RETIRE |

**TUI and operator surfaces** (all LAUNCHER L3/L4, reading the record instead of the run folder):

- `sandbox watch` Tray TUI (`ms/watch.py` 1,136): live terminal view of one run; `--web` browser tray
  (`ms/tui/web.py` 996); job-aware labels (`tui/tray_context.py` 515); target resolution (`watch_target.py` 268).
- Watchdog alerts from run state and logs (`tui/watchdog.py` 283).
- `sandbox board` (`tui/board.py` 447): one persistent display of every running job, fed by `mailroom.beacon/v1`
  heartbeats (`tui/beacon.py` 140, `services/beacon.py` 24) and the services strip (`tui/services_panel.py`,
  `services_snapshot.py` `mailroom.services/v1`, `services/poll.py`); macOS autostart plist (OPEN).
- Themed output for long CLI jobs (`tui/session.py` 220) and the retro log monitor (`tui/pretty_log.py` 2,367).
- `sandbox tui` interactive shell with nested menus, completion and history that dispatches the parser, never a
  system shell (`tui/shell.py` 432).
- `sandbox floor` resident floor viewer (`tui/floor_app.py` 484, `reports/dashboard/floor.template.html` 529,
  digest of all runs `build_floor_digest.py` 1,067 with its self-test 744), recorded replay of any run (2.10),
  options and run discovery, live refresh, analysis panel (scores, documents, nodes) per `docs/floor.md`.
- `sandbox dev` synthetic run for the watch dev server (`tui/demo.py` 210).
- Screenshot harness for docs (`scripts/sand032/capture_watch_screenshots.py` 602, synthetic data only).

**Merge plan in one line:** the reloaded browser terminal is the shell; each sandbox feature group becomes a
`registerModule` plugin (run, experiment, autoresearch, services, floor link), and the Python terminal surfaces
(tray, board, shell, pretty log) stay Python under `mailroom_launcher.tui`, both reading the same record API.

## 4. Proposed chunks

### 4.1 Launcher chunks (stage 4; L1 to L5 as in `2026-11-18-workspace-port-inventory.md` section 3 and 5, L6 to L9 added)

Sizes are old lines to read and port (not new code). `metrics.py` (2,119) and `run_gpu_cost.py` (1,366) are split
across chunks and counted once below, under the chunk that takes most of them. The workspace port design lets the
chunks that need no engine (L1, L6, L3, L4) start before the go/no-go gate and work over imported runs only (owner
decision 5); L2, L7, L8 and L9 wait for the engine.

| Chunk | Sandbox sources | Old lines | Other sources | Depends on | Proof it can show |
|---|---|---:|---|---|---|
| **L1 Run spec, draws, preflight** | `job/spec.py`, `job/preflight.py`, `job/context_plan.py`, `context_fit.py`, `job/suite.py`, `eval/primary.py`, `corpus.py`, `dataset_registry.py`, `datasets.py`, `prep.py`, `scripts/build_excluded_ids.py`; the dataset/fleet locks in `metrics.py` (`lock_dataset_provenance` 535-567) | 5,120 (+ part of `metrics.py`) | llm-mailroom L1 rows (8,493: `hf_corpora`, `run_pilot`, `run_experiment`, `cutover`, ...); `config/runs/` (225 files) | record `models` (Run header fields incl. policy hash, kind, backend, signer, label snapshot ids); labels package (snapshot ids); client profile + prompt sets (inventory step 10). **No engine.** | Spec round-trip and canonical-hash property tests; every one of the 225 sandbox run YAMLs either converts or is listed with the reason; preflight fault matrix (writer busy, foreign checkout, draw drift, missing price row, unreachable probe) each with its exact refusal; mutation: drop one check, a test goes red |
| **L6 Spend gate, caps, price sheet, ledger** | `job/spend.py`, `job/api_budget.py`, `job/budget_advice.py`, `job/ledger.py`, `scripts/estimate_spend.py`, `scripts/refresh_api_prices.py`, `scripts/refresh_decision_models.py`; pricing in `metrics.py` (91-471) | 1,189 (+ ~400 of `metrics.py`) | `config/api_prices.yaml`, `config/decision_models.yaml`; llm-mailroom `cost_models` | L1, Dojo D5 (creates `config/prices/`) | No paid action without a fresh receipt: non-TTY without initials refuses, bad initials, receipt older than 24 h, changed fingerprint, unpriced model, cap below estimate, key headroom below reserve, each an exact error; signer lands on the Run; price-sheet `--check` against a recorded catalog fixture (no network in tests) |
| **L2 Backends** (OpenRouter, Modal with vLLM, Compose, local) | `deploy/modal_vllm.py`, `deploy/modal_job.py`, `deploy/publish_modal_image.py`, `job/remote.py`, `job/deploy_env.py`, `job/container_telemetry.py`, `job/vllm_metrics.py`, `job/benchmark_check.py`, `modal_matrix.py`, `compose.py`, `providers.py`, `health.py`, `tunnel.py`, `agent_routes.py`, `mock_llm.py`, `services/*` (10), the driver half of `run_gpu_cost.py` | 6,001 (+ part of `run_gpu_cost.py`) | llm-mailroom L2 (1,404: `llm/providers`, `openrouter_utils`, smoke scripts) and `deploy/` (1,693, incl. the producer Compose file); reloaded `deploy/mock_openai.py` (67) | L1, L6; engine E1 fake model and E6 API (Compose producer image); record writer | **Stage-4 gate part 1:** one mock run per backend with no spend, each writing a record that verifies: OpenRouter against a local fake HTTP server, Modal in local/stub mode, Compose against the producer image with the fake model, local against the fake OpenAI server. A field a backend cannot report is recorded as absent (asserted per backend) |
| **L7 Run driver** | `job/runner.py`, `eval/runners.py`, `eval/agents.py` | 3,625 | - | L1, L2, record log API; engine E2/E3 | Killed-process resume (`kill -9` mid-run) does not repeat a recorded call; pause/cancel handshake; concurrency with per-test `tmp_path`; mutation in the resume cursor turns a test red |
| **L3 CLI and TUI** | `cli.py`, `tui/*` (minus floor), `watch.py`, `watch_target.py`, `scripts/check_cli_surface.py`, `scripts/sand032/capture_watch_screenshots.py` | 12,513 | reloaded `api/tui/` (5,714 lines JS/CSS/HTML), `api/routes/ready.py` (305), the read routes of `api/app.py`, `schemas/replay.py` (216); The-Mailroom `tui/` (per `2026-11-18-workspace-port-inventory.md` section 4) | record index, trace and tail (record Tasks 7 to 9); L1, L6 (commands that start runs) | CLI surface check: every sandbox command in 3.4 maps to a new command or a listed retirement; reloaded JS tests ported and green; screenshots from a synthetic record only |
| **L4 Floor server and web floor** | `tui/floor_app.py`, `ports.py`, `reports/dashboard/build_floor_digest.py`, `selftest_floor_digest.py`, `floor.template.html` | 2,305 (+529 HTML) | reloaded `obs/replay/sessions.py` (308), `schemas/replay.py`; The-Mailroom `server/` and `web/` floor | record replay timeline (Task 10b), L3 server | The floor replays an imported v1 run; the record timeline equals sandbox `build_timeline` on the same folder (event order, terminal state, meta) as a differential test; the `replay/v1` adapter renders in the reloaded viewer |
| **L5 Operator desk** | - | - | The-Mailroom `operator_desk/` (per `2026-11-18-workspace-port-inventory.md` section 4) | L4, review station | its own gate (design section 8): JWT refusal paths tested (missing, expired, wrong-role token); no document text in desk logs (log-scan test); desk store kept apart from the record index (boundary test); archive file access read-only (`fitz`) |
| **L8 Experiments and autoresearch driver** | `autoresearch/loop.py`, `levers.py`, `estimate.py`, `cli.py`, `parity.py`, `__init__.py`, `experiments.py`, `eval/matrix.py` | 3,737 | `config/autoresearch/`, `config/experiments/` | L1, L6, L7; Dojo D6 (utility) and D7 (keep rule, paired delta, noise floor) | `config/autoresearch/mock-smoke` study runs end to end on the mock backend at zero spend; re-deciding the stored v1/v2 rounds from their stored scores reproduces the sandbox's keep/revert decisions, except where the R2 per-field guard (design section 4.2) reverts them, each such case listed; study budget and reservation tests (crash between reserve and run) |
| **L9 Stage-4 gate** | `scripts/smoke_campaign.py` | 121 | - | L1 to L8 | Design gate: a mock run per backend with no spend (script, report under `docs/reports/` written by the script); then one paid parity run per paid backend (OpenRouter, Modal) **started by the owner** through the spend gate with the owner's initials |

Launcher total (old lines, sandbox only): about 34,800 kept (verdict LAUNCHER) plus parts of the two SPLIT modules.

### 4.2 Dojo chunks that receive sandbox code

| Dojo chunk | Sandbox sources | Old lines | Notes |
|---|---|---:|---|
| **D3b** tasks, suites, document scorers | `eval/scoring.py` (dispatch through the Dojo; the JSONL sink retires); `eval/cuad_scoring.py`, `maud_scoring.py` retire (adapters to code D3b already owns) | 724 | the 89 doc-scorer goldens were captured from this sandbox (`264153a5`), so `eval/scoring.py` is the reference caller |
| **D4** label layer | `eval/extraction_scope.py`; `job/gate.py:labelled_count` 163-179 and `min_labelled` (the scorable-field count the design names) | 271 (+ part of `gate.py`) | the label layer decides scorability, the gate consumes it |
| **D5** run layer | `eval/human_review.py`, `eval/schema_adherence.py`, `eval/serving_parity.py`, `serving_groups.py`, `job/grid_cards.py`, `job/grid_master.py`, `node_trace.py` summaries, `metrics.py` (`record_from_run`, `enrich_serving_report`, `serving_record_from_store`, `compare`, `per_document_rates`) | 3,745 (+ ~1,200 of `metrics.py`) | de-hardcode SAND-037 postures (they become run-spec presets); `grid_master` uses scipy |
| **D6** document outcome and behaviour | `job/gate.py`, `gate_causes.py`, `gate_render.py`, `routing_audit.py`, `autoresearch/scoring.py:doc_utility` + `UtilityWeights` | 1,110 (+ part of `scoring.py`) | design proof 2 and 3 (v1 6/5/7/7 and 5/5/8/7; r2 40 of 55 and 9 of 95) are computed by exactly this code today: run it on the stored folders before porting to freeze the expected numbers |
| **D7** comparison, keep rule, projections, cost truth, log health | `autoresearch/keep.py`, `diagnose.py`, `report.py`, the rest of `scoring.py` (`RunScores`, `paired_delta`, `usd_per_utility_point`, `agent_cost`), `job/pair_report.py`, `gate_compare.py`, `gate_audit.py`, `funding.py`, `api-evals/api_evals/cost.py`, `scripts/calibrate_reviewer.py`, `extraction_gate.py`, `compare_sorter_prompt.py`, `compare_specialist_prompt.py`, `ladder_pair_report.py`, `reports/dashboard/breakeven.py`; `metrics.py` `extrapolate_cost`, `estimate_suite`; the curve half of `run_gpu_cost.py` | 2,792 (+ splits) | keep rule's per-class noise guard already exists (finding 3) |

Dojo totals from the sandbox: 27 modules / 9,030 lines HANDS TO DOJO plus the Dojo halves of the two SPLIT modules.
None of them may import launcher modules; the `cuts:` column in section 1 lists the imports to remove
(e.g. `gate.py` -> `paths`, `gate_causes.py` -> `pair_report`, `calibrate_reviewer.py` -> `checkpoint`, `spec`).

## 5. Third-party dependencies and their fate

### Sandbox (`pyproject.toml`, version 0.3.0)

| Dependency | Where it is used | Fate |
|---|---|---|
| `pyyaml` | 29 modules | launcher core (spec, config) |
| `pydantic` | 7 modules (spec models) | launcher core (already a record dependency) |
| `httpx` | 13 modules (probes, Modal URLs, OpenRouter key) | launcher core |
| `python-dotenv` | `dojo_source`, `providers`, `runtime` | drop: secrets come from the environment only |
| `structlog`, `openai` | base deps, used by the vendored trees | drop from launcher (`mailroom_common.logging`; `openai` is the engine's client) |
| `numpy`, `pandas`, `matplotlib`, `openpyxl` | base deps "for the vendored Dojo"; `sand42_figures`, `grid_figures`, `grid_reader*`, `gold_labels` script | drop from launcher; reports that keep a chart go to a Dojo `reports` extra |
| `scipy` | base dep; `grid_master` | Dojo (already core there) |
| `markdown-it-py` | `grid_reader_pdf`, `pages_site` | drop (retired modules) |
| `huggingface_hub`, `pyarrow` (extra `hf`) | `corpus`, `deploy/modal_vllm.py` | launcher `hf` extra (read only); pyarrow is already a record dependency |
| `modal==1.6.3` (extra `deploy`) | 6 modules | launcher `modal` extra; reloaded pins `modal==1.5.5` and llm-mailroom has its own extra: pick one pin (finding 7) |
| `pydantic-ai-slim[typesafe]==2.68.0` (extra `decision`) | `decision_backends` | engine extra for decision slots (E3) |
| extra `pipeline` (`langchain-core`, `langchain-openai`, `langgraph`, `fastapi==0.162.6`, `python-multipart`, `langgraph-checkpoint-sqlite`, `requests`, `watchdog`, `sqlalchemy`, `aiosqlite`, `greenlet`, `pypdf`, `pdfplumber`, `pillow`, `markdown-it-py`) | the vendored llm-mailroom tree | engine or drop, as decided in `2026-11-18-workspace-port-inventory.md` section 3; `fastapi` also a launcher server dependency |
| extra `observability` (`langfuse`, `braintrust`, `opentelemetry-*`) | `eval/tracing.py`, `job/otel.py` | optional engine exporter extra; `braintrust` drops |
| extra `notebooks` (`jupyterlab`, `ipykernel`, ...) | notebooks | drop |
| `playwright` (not declared) | `scripts/sand032/render_modal_performance.py` | drop (retired); screenshot evidence uses it outside the package |
| dev: `pytest`, `pytest-mock`, `pytest-cov`, `pytest-xdist` | tests | keep `pytest`, `pytest-xdist` (workspace rule) |

### mailroom-reloaded (`pyproject.toml`, version 0.4.2)

| Dependency | Fate |
|---|---|
| `crewai`, `openinference-instrumentation-crewai` | drop (second engine retires) |
| `fastapi`, `uvicorn` | launcher server for `/tui`, `/ui`, the floor and SSE |
| `typer` | drop: the sandbox CLI is argparse and is the larger surface; keep one parser library |
| `sqlalchemy`, `pydantic-settings` | drop (record index chooses its driver; settings come from config) |
| `opentelemetry-sdk`, `-exporter-otlp`, `openinference-instrumentation-openai` | optional engine exporter extra |
| `pypdf`, `pdfplumber`, `pymupdf`, `python-docx`, `reportlab`, `watchdog` | engine (as in `2026-11-18-workspace-port-inventory.md`) |
| `jellyfish`, `python-dateutil`, `numpy`, `scipy`, `sentence-transformers` | Dojo (already decided there) |
| `scikit-learn` (dev), `datasets` (eval) | OPEN with Jev / drop (`huggingface_hub` covers reads) |
| Google client stack (extra `gmail`) | OPEN with Gmail intake |
| `mailroom-ml[serve]` (extra `bert`), `llm-dojo-scoring@v0.21.0` (extra `parity`) | drop (ML contract stays on 0.30; no Dojo pin) |
| `modal==1.5.5` | see the sandbox row |
| JS: none (no npm); tests run on Node's built-in runner | keep "no npm"; CI image needs Node (finding 8) |

## 6. Findings and open questions

1. **Compose backend source.** The design's Compose backend is "the producer image with API and watcher". The
   sandbox's `deploy/docker-compose.yml` serves models and tracing (`ollama`, `vllm`, `llamacpp`, `phoenix`,
   Langfuse stack) and `compose.py` only wraps `docker compose`; the producer image lives in llm-mailroom `deploy/`
   (counted in L2). L2 needs both: the producer image (engine E6) and the local serving services.
2. **Node status vocabulary.** The sandbox writes node status `error` (`node_trace.py` `_emit`); R7 asks for
   `failed`. The importer and the record replay timeline must map one to the other and say so (record agent).
3. **Keep rule already has a per-class noise guard.** `keep.py` 34-86 at `93f298c` limits a class drop to
   `max(max_class_drop, class_noise[cls])` from the replicate baseline. The design says the guard is to be
   "rewritten to respect per-class noise" (the v1 guard fired on noise); check whether `93f298c` is that rewrite
   before porting, so D7 does not redo it.
4. **What the launcher drives before the engine exists.** The workspace design lets L1, L6 and the operator
   surfaces start before the go/no-go gate "because the launcher must also drive the current pipeline until
   cutover". Today the sandbox drives it by importing a vendored llm-mailroom (`runtime.py`, `eval/agents.py`,
   `eval/runners.py`, `family_api.py`, `mock_llm.py`); the workspace forbids vendored trees. Owner decision:
   (a) a temporary "legacy driver" backend that installs llm-mailroom 0.30 as a pinned package and writes records,
   or (b) the run driver L7 waits for the engine and the early launcher only works over imported runs.
5. **Two replay contracts.** R7 names the sandbox `build_timeline` (flat `checkpoint` / `node` / `item` events). The
   reloaded viewer reads the much richer `replay/v1` payload (`schemas/replay.py`: `stations`, `entities`,
   `segments` with attempt/retry/tokens/cost, `generations` with model, prompt version, tokens, TTFT,
   `schema_valid`, `events`, `scores`, `rollups`). The record timeline should carry what `replay/v1` needs, or the
   launcher needs an adapter that computes it from record events; tell the record agent now (Task 10b).
6. **Study-specific code is large.** 64 sandbox modules / 18,677 lines retire, mostly SAND-032/037/042 reports,
   the mailroom-issues hub, vendor/snapshot plumbing and the subagent roster. The design's rule ("their tables
   become Dojo reports rebuilt from records") means no report is ported until a gate or the owner asks for it.
7. **Pins to reconcile:** `modal` (1.6.3 sandbox vs 1.5.5 reloaded), `fastapi==0.162.6` (sandbox) vs unpinned
   (reloaded).
8. **Node in CI.** Keeping the reloaded TUI tests means the CircleCI image needs Node (23 `.mjs` tests), a
   governance change outside the launcher package.
9. **Layout map.** `config/runs/`, `config/backends/`, `config/datasets.yaml`, `config/autoresearch/`, the price
   sheet, and `packages/launcher/src/mailroom_launcher/{spec,preflight,spend,backends,driver,draws,tui,floor,server,autoresearch}`
   need lines in the R9 map before they are created.
10. **Network reads in scripts.** `refresh_api_prices.py` and `refresh_decision_models.py` read the public
    OpenRouter catalog (no write, no key) and `hf_cli.py` / `corpus.py` read the Hub; tests must use recorded
    fixtures. `gt_labeler.py` / `deploy/modal_gt_labeler.py` make model calls (labels project; owner go-ahead).
11. **Ledger in git.** The sandbox tracks `reports/run-ledger.md` in git and rewrites its result cell. In the
    workspace the signer and result live on the Run in the record; any Markdown ledger is a generated view, and
    `docs/reports/` is script-written only.
12. **Not verified:** that every one of the 225 run YAMLs still loads under the sandbox's own `load_run_spec`; the
    sandbox and reloaded test suites were not run (read-only inventory); `deploy/htcondor/` and the launchd plist
    usage was not traced.

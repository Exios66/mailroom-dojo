# Workspace port: system design for the remaining packages

Date: 2026-11-18. Status: proposed, for the owner's review. Serves the master design
`docs/specs/2026-11-18-lean-rewrite-design.md` (R1 to R10, "Build order and proof gates",
"Systems review"). Where this document and the master design disagree, the master design wins
and this document is wrong, except where section 10 records an owner decision that changes it.

The module-by-module inventories behind every number here are
`docs/plans/2026-11-18-workspace-port-inventory.md` (old Dojo, llm-mailroom, The-Mailroom) and
`docs/plans/2026-11-18-launcher-inventory.md` (the sandbox and mailroom-reloaded). Where an
inventory's verdict or module name differs from this document, this document wins.

## 1. Scope

What is left to move into this workspace, after record (project 1), labels (project 2) and the
Dojo scoring core (project 3, chunk 1):

| Source | Physical lines (tests excluded) | Destination |
| --- | ---: | --- |
| `llm-dojo-scoring` 0.30 (old Dojo) | 31,450, of which 5,385 already ported | `packages/dojo`, `config/clients/<client>/` |
| `llm-mailroom` `src/` (pipeline) | 80,124 | `packages/engine`, `config/policies/`, `config/prompts/`, some Dojo, some launcher |
| `local-mailroom-sandbox` (**the launcher**, as the design says) | 79,218 (207 modules) | `packages/launcher`, some Dojo, the record importer |
| `mailroom-reloaded` (TUI base, R7) | 29,470 Python plus 6,338 browser TUI | `packages/launcher` (TUI) |
| `The-Mailroom` (floor, operator desk, server) | 15,926 Python plus about 11,100 front-end | `packages/launcher` (operator surfaces) |

Out of scope here, owned by other threads: the human review station (the desk plan and its
decision record, on that thread's branch) and the run log storage (the record's importer, floor
replay timeline, pack and prune: record plan Tasks 10 to 12, ending in the stage-1 gate). This
document names where they plug in and nothing more.

### Principle: one workspace for all functionality, rebuilt better

mailroom-dojo is the one workspace for **every capability** the constellation repositories
provide today (old Dojo, llm-mailroom, the sandbox, mailroom-reloaded, The-Mailroom),
refactored and improved, not copied. Two rules from the master design decide the form each
capability takes:

1. **Every capability has a decided fate.** If an operator, a client or an experiment uses it
   today, it either has a home in exactly one package (or in `config/`), behind the one-way
   boundaries, or it is listed in section 7: dropped by the master design by name, or proposed
   for retirement or still open, for the owner to confirm. Nothing is left behind silently. The
   old repositories stay readable on their pins until cutover (master design, non-goals) and
   then take fixes only.
2. **Code survives only if something reads its output** (master design, "What is dropped" and
   the "second bloat" anti-goal). A capability is kept; the old module that implemented it is
   kept only when it is the best way to provide it. So most old code is replaced: storage and
   audit by the record, trace readers by the record, Python tables by configuration, eleven
   honesty call sites by one `Measure` type, a 22-node graph module by a policy file.

The master design drops these by name: LegalBench scoring, the metric registry's tiers, bundles
and profiles, the legacy flat import paths, the release-awareness tool, the mailroom-ml label
contract (it stays on the 0.30 line with its user) and the sandbox's study-specific report
modules (their tables come back only as Dojo reports rebuilt from records). Everything else the
master design does not name is in section 7 with a proposed fate. Lines are a size estimate, not
a target; about a third of the old lines are replaced, not ported (section 7).

What "significantly improved" means here, each with its proof:

| Old pain | Improvement | Proof |
| --- | --- | --- |
| Pins, vendored snapshots, scoring split three ways | One workspace, one version, one CI, one scorer | boundary test; no `vendor/`; no commit pins |
| Global settings, env-var config, lru caches | Immutable profiles passed in; tuning is hashed config | no-global-state tests (Dojo chunk 1 pattern) |
| Several stores to reconcile, global chain lock, lost records | One append-only record, per-document chains, derived index | record fault and property tests |
| Graph shape in 8,900 lines of code | A policy file that draws and runs the graph | `engine graph --policy legacy`; router replay |
| Visualizer and TUI driven by hosted traces | Every operator surface reads the record; traces are optional exports | engine runs with no trace sink |
| Numbers without sample size or label status | `Measure` on every number | a report cannot print a bare float |
| New document type needs code in five places | A profile entry, a schema file, a prompt, a scorer config | the synthetic-type test in every package |

## 2. Requirements this design must meet

Functional, from the master design:

- **One scorer** (Dojo) that reads only records and label files and never runs a model or imports
  the engine.
- **One engine** whose graph shape is a policy file, compiled to LangGraph, with the record as the
  only checkpoint and an exact replay client.
- **One launcher** (what the sandbox becomes) that starts runs and writes records and scores
  nothing; it owns run specs, preflight, the spend gate, backends and the operator surfaces (CLI,
  TUI, floor).
- **New document type by configuration only** (R4), proved end to end in each package.
- **Provider-agnostic** (R5): no provider or model name in agents, prompts or policies.

Non-functional, as numbers (master design, "Systems review"): at least 249 documents a minute with
the fake model and durable writes; a crash loses at most the step in flight; resume never repeats
a paid call; any stored run re-scored in minutes on a laptop with no model call; wrong-archive at
most 2% per class where reachable; team of one owner, one collaborator and agents.

Constraints: one-way dependencies enforced by test; tuning lives in `config/` and `labels/`;
CircleCI only; no real model call in tests; no hosted writes without the owner; the engine waits
for the go or no-go gate after stage 2; `legacy` is the 0.30.0 tag after #309 and #310.

### 2.1 Traceability: R1 to R10

The master design asks every project plan to name the requirements it serves and how its gate
checks them (spec line 9). Chunk numbers are those of section 6.

| Req | Served by | How the gate checks it |
| --- | --- | --- |
| R1 fit AmFam, be a benchmark | D5, D6, D7, project 4, cutover | Stage-2 gate reproduces v1 and slice-100 r2 (r2 only once the owner supplies its folder; section 6 gives the fallback); Set F pass rule on slice-500 (approved 2026-11-17): wrong-archive and review rates with bounds, cost per 1,000 documents, the 2% bar per class as pass or assisted-mode |
| R2 extraction quality | D3b, D4, D5, D7, P2, P4 | Goldens unchanged (D3b); per-field, per-class accuracy and coverage reported separately for every run (D5); the keep rule reverts a candidate that lowers any class's per-field accuracy or per-field coverage beyond noise even when utility rises, with every scored class guarded (D7 tests, L8 re-decision) |
| R3 classification with hand-off | D5, D6, E3 | Class accuracy, silent-wrong per 1,000 and cost per classification on v1 (100 of 100 reproduced); the hand-off's effect on extraction reported on v1 and r2 (r2 once supplied); `legacy` keeps sorter-to-specialist as its default shape (router replay) |
| R4 new type by configuration | D3a, D3b, E2, E3, L1 | The synthetic-document-type test in labels, Dojo, engine and launcher: a new type added by profile entry, schema file, prompt and scorer config, no code edit |
| R5 provider-agnostic, every launch path | E1, E6, L2, P4 | A test that finds no provider or model name in agents, prompts or policies; the HTTP API launch path: a mock upload through the E6 API writes a record that verifies (E6 gate), and the Compose backend's stage-4 mock run goes through that API (L2); the stage-4 mock run per backend (OpenRouter, Modal with vLLM, Compose, local); a prompt variant holds its gain on two worker models before it becomes the default |
| R6 labels from the dataset and Claude labels | labels (project 2), D4, R-H | D4 checks the Dojo's Protocols against the real `LabelSet`; every scored run names the label snapshot it was scored against: a native run in the `Run` header's `label_snapshot_ids` (on `main`), set from its run spec; an imported sandbox run has an empty list there, so the Dojo report records the snapshot instead (section 3, D5). Once R-H lands (section 3), the header also carries the client-profile hash and the engine schema version and hash it ran under; a content-hash mismatch is reported as unverified, never scored silently |
| R7 CLI, TUI, floor replay | record replay timeline (chunk 0), L3, L4 | L3: every sandbox command maps to a new command or a listed retirement (section 10); reloaded JS tests green; L4: the record timeline equals sandbox `build_timeline` on the same folder |
| R8 tests that detect failure | every chunk; section 6.1 | Each chunk's PR shows its fault, property or stateful tests and one deliberate mutation turning a test red; each package has its own test strategy |
| R9 layout and boundaries | section 3; every chunk | Each package's boundary test; `make boundaries` and `make layout`; a new folder has a map line first |
| R10 decision slots by configuration | record decision events, D6, E3, L8 | D6 reports, for each of the eight slots on synthetic decision events, the master design's list (spec line 123): per-slot accuracy and override rate, silent-wrong per 1,000, cost per decision and the risk-coverage curve with bounds, plus cap use; `legacy` holds all eight slots at their baseline; Sets A, B, D and E scored |

## 3. High-level design

```
                  config/clients/<client>/          config/policies/      config/prompts/<set>/
                  (classes, schemas, scoring,       (legacy + arms)       (deploy_v1, overlays)
                   thresholds, label snapshot)            │                      │
                        │        │                        │                      │
                        │        └──────────────┐         │                      │
 labels/<snapshot>/ ──► mailroom_labels         │         ▼                      ▼
                        │ (resolve, gate,       │   mailroom_engine ◄──── run spec ◄──── mailroom_launcher
                        │  coverage)            │   (agents, handoffs,      (backend,     (run specs, preflight,
                        │                       │    policy compiler,        policy,       spend gate, backends,
                        ▼                       │    replay client,          draw)         CLI, TUI, floor,
                   mailroom_dojo ◄──────────────┘    queue, API)                           autoresearch driver)
                   (labels, fields, outcome,              │ writes                               │ writes, reads
                    behaviour, run, confidence,           ▼                                      ▼
                    stability, cost truth,        ┌────────────────────────────────────────────────┐
                    log health, comparison,  ◄────│ mailroom_record: event log (only truth), blobs, │
                    projections, reports)  reads  │ index (derived), seals, verify, trace, replay   │
                                                  └────────────────────────────────────────────────┘
 shared leaves (PR #6): mailroom_common (hashing, ids, JSONL, YAML, redacting logs, retry, model client
 seam), mailroom_dictionaries (closed vocabularies) — importable by every package above record.
```

Data flow for one run: the launcher resolves a run spec (client profile hash, engine schema
version and hash, policy hash, prompt set and overlays with hashes, label snapshot manifest hash, draw,
backend, caps) and hands it to the engine; the engine writes every step, handoff, decision and model response to the record
(blob first, then the event, then the index); the Dojo later reads the sealed record plus the
label snapshot named in the run and writes reports. Nothing flows back from the Dojo to the engine
except through a human or the autoresearch driver choosing the next policy or run-spec value (for
example merger parking, P3b: a person or the driver sets the bar status in the policy or run spec
from the Dojo's bar-check report; the engine never reads a report).

**This needs a record change, R-H.** The `Run` header on `main` (`packages/record`,
`mailroom_record/models.py`, `CURRENT_RUN_VERSION = 1` in `upgrade.py`) carries `policy_hash`,
`prompt_set`, `prompt_hashes` and `label_snapshot_ids`, but no client-profile hash and no schema
version or hash. R-H is a Run header version bump (to version 2, with an upgrader from
version 1) that adds `client_profile_hash`, `engine_schema_version` and `engine_schema_hash`.
The header's own `schema_version` is not reused: it is the record format version of `run.json`
(the one R-H bumps from 1 to 2, read by `upgrade_run`), not the engine schema a run used. It is a
record-schema change, so tier 3 (three adversarial rounds), owned by a new chunk in the record
build plan's follow-ups, not by any Dojo or launcher chunk. P2 and P5 wait for it (section 4.5).
For an imported sandbox run, the importer takes the thresholds the run used from the sandbox's
`spec.lock.json` `confidence` block, which preflight writes only when the run spec set one
(`job/preflight.py` line 1141, at `93f298c`; without it the run used the taxonomy defaults that
`job/routing_audit.py` `effective_thresholds` merges), and records the client-profile values and
hash from them; the `engine_schema_version` of an imported run is the frozen `legacy` engine
schema version (section 4.1). How the importer hashes those values into `client_profile_hash` is
for the record work to verify.

**The label snapshot of an imported run.** The importer writes `label_snapshot_ids=[]`
(`mailroom_record/importer/sandbox_run.py`) and R-H keeps it so: a sandbox run used no snapshot
of this workspace, and which snapshot will score it is decided later, while the header is
immutable. So the header alone cannot meet R6 for an imported run, and the run reader (D5) does:
it takes the snapshot to score against explicitly (never a default), scores a native run only
against a snapshot its header names, and stamps every report with the snapshot's `snapshot_id`
and `manifest_hash` (the Dojo's `LabelSource` Protocol, which `mailroom_labels`' `LabelSet`
meets). The R6 provenance of a scored run is the
header plus that stamp.

### Package boundaries (the enforced rule)

| Package | May import (ours) | Never |
| --- | --- | --- |
| `record` | nothing (adopting `common` is a planned follow-up) | anything domain-specific |
| `labels` | `common`, `dictionaries`, `record` | `dojo`, `engine`, `launcher` |
| `dojo` | `common`, `dictionaries`, `record`, `labels` | `engine`, `launcher`, network, env vars |
| `engine` | `common`, `dictionaries`, `record`; reads `config/` | `dojo`, `labels`, `launcher` |
| `launcher` | all of them | nothing else of ours exists above it |

**Approved deviation (owner, 2026-11-18).** The master design's map names five packages. PR #6
adds two shared leaves, `common` and `dictionaries`, with map lines and a boundary script; the
owner decided to keep them. They keep the one-way rule (they sit below everything) and remove
copy-paste of hashing, JSONL, redacting logs and closed vocabularies. The owner also adopted
PR #6's `make lint` (ruff), `make typecheck` (mypy strict) and `make spell` (codespell) gates;
`CLAUDE.md`, `AGENTS.md` and the rules now say so.

## 4. Deep dive

### 4.1 One schema source per class (R4, the critical path)

Today a class's fields are listed twice: `config/clients/mailroom-dataset/profile.yaml` (labels)
and `scoring.yaml` (Dojo), and the engine will need them a third time for specialists and prompt
alignment. Three copies drift. Design:

```
config/clients/<client>/
├── profile.yaml        classes (key, status live|retired, sorter description, subclasses,
│                       thresholds), label snapshot, hub revision; names each class's schema file
├── schemas/<class>.yaml   one file per class, two sections (below): `scored` (labelled and
│                          scored fields) and `extracted` (what a specialist returns, per
│                          engine schema version)
├── scoring.yaml        Dojo-only scoring rules (bands, audits, trace capture, list matching),
│                       no field lists
└── bars.yaml           the archive gate's bars: document quality bar, wrong-archive and
                        review maxima, gated classes (D6; from the sandbox's
                        `config/production/bars.yaml`)
```

**Extracted and scored fields are separate sections.** The old code keeps two field tables, and
they disagree, so no single list can equal both. Checked at the reference commits: for
`corporate_record`, the old Dojo scores 40 fields (the 9 `doc_classes` fields plus the 31
subclass entity fields of `corporate_record_schema`, at `23f8234`; the workspace profile
`config/clients/mailroom-dataset/profile.yaml` has the same 40), while llm-mailroom's
`EXTRACTION_SCHEMAS` (`src/schemas/documents.py` line 160, at `070663f`) asks for 9, and
`acting_body` appears nowhere in llm-mailroom `src/`; for `contract`, the profile labels
`cuad_clause_labels` (a never-scored annotation key in `scoring.yaml`), while llm-mailroom
extracts `maud_clauses` and `merger_consideration`, which the Dojo accepts but never scores
(`accepted_unscored_fields` in `scoring.yaml`). So each `schemas/<class>.yaml` has:

- **`scored`**: the fields that are labelled and scored, each with its type (Dojo field types),
  vocabulary ref, coverage rule, containment, scoring status (scored, accepted-unscored or
  never-scored annotation key) and variants (e.g. the CMS row variant). Labels and the Dojo read
  it.
- **`extracted`**: the fields a specialist is asked to return, keyed by engine schema version.
  The engine, the prompt sets and the alignment check (P1) read it. `legacy` pins a frozen engine
  schema version whose extracted section equals llm-mailroom's `EXTRACTION_SCHEMAS` at the legacy
  reference (`070663f`, re-checked against the 0.30.0 tag once X2 lands). A new engine schema
  version (P2's merger questions, for example) is added beside it; the frozen one never changes.

A field may sit in both sections or in one only; a field the Dojo scores and the `legacy`
specialist never extracts (`acting_body`) is a known gap, reported, never an error.

Labels, Dojo and engine each read `schemas/<class>.yaml` through their own small loader (no shared
loader package needed; the file format is the contract, versioned with `schema_version`, and a
workspace test loads it with all three). Old Python tables become these files, and the D3a gate
names which old table each section must equal: the `scored` section equals a frozen JSON dump of
the old Dojo's tables (`doc_classes`, `cuad_schema`, `maud_schema`, `corporate_record_schema`,
`extraction_schemas` at `23f8234`), as chunk 1 did for field types; the `legacy` extracted
section equals a frozen JSON dump of llm-mailroom's `EXTRACTION_SCHEMAS` at `070663f`. This is
Dojo chunk D3a, and it unblocks the engine's specialists, the prompt sets and the merger schema
(P2).

### 4.2 The Dojo's remaining layers

The master design's "What it scores" table is the module layout. Each layer is a pure function
from typed inputs to `Measure` values; the record reader is the only place that touches storage.
These folder names are the ones both inventories use.

```
mailroom_dojo/
├── profile/, values/, normalization/, field_scoring/, classification/, labels_interface/   (done)
├── statistics/        bootstrap, calibration (ECE, ranking power), paired tests        D1
├── measures/          Measure value type + the flat metric table loader (headline and
│                      supporting metrics; every number carries n, cost basis, label status) D2
├── schemas/           loader for config/clients/<client>/schemas/<class>.yaml          D3a
├── tasks/, suites/,   task kinds; per-agent suites; per-document scorers dispatched
│   document_scoring/  by profile                                                       D3b
├── label_layer/       scorable-field rules over mailroom_labels.LabelSet                D4
├── run_reader/        typed view of a sealed record (documents, steps, decisions, costs) D5
├── run_layer/         class accuracy, silent-wrong per 1,000, cost per classification,
│                      per-field and per-class accuracy and coverage (R2), cost per
│                      agent/class/outcome, schema adherence, cards, serving             D5
│                      wrong-archive and review rates with bounds, the risk-coverage
│                      curve with bounds (they need the archive gate)                    D6
├── confidence/        calibration error and ranking power per agent and class; threshold
│                      refits after a prompt change                                      D5, D7
├── outcome/           archive gate, cause codes, document utility, the gate's ranking
│                      power                                                             D6
├── behaviour/         the eight decision slots: per-slot accuracy, override rate,
│                      silent-wrong per 1,000, cost per decision, risk-coverage with
│                      bounds, cap use; routing audit; the hand-off's effect on
│                      extraction (R3)                                                   D6
├── compare/, projections/, stability/, cost_truth/, log_health/                         D7
└── reports/           report builders (registered), written by scripts under docs/reports/
```

The price sheet is configuration at `config/prices/`. D5 creates it, with its layout-map line in
the same PR, when the old Dojo's hard-coded token prices (`analysis/cost.py`) become a price sheet
for the run layer's cost figures. Later readers use that same folder: the launcher's spend gate
and price-sheet check (L6) and the Dojo's cost truth (D7). L6 adds the sandbox's
`config/api_prices.yaml` entries to it; it does not create a second sheet.

Key decisions:

- **The headline registry metrics stay, improved (owner, 2026-11-18).** "Headline" means the old
  registry's tier 0 (llm-dojo-scoring `llm_dojo_scoring/metrics/registry.py` at `23f8234`): 14 ids,
  often several per agent, although the module docstring says "one number per agent". They are
  `f1_macro` and `accuracy` (all agents); `extraction_overall_score` (specialists, reporter, boss);
  `extraction_f1` and `extraction_f2` (specialists); `content_topic_f1_macro` (correspondence
  specialist); five `reviewer_*` ids (sorter reviewer: class accuracy, wrong-accept rate and its
  upper 95% bound, routed-to-review rate, accept-rule violations); `ttft_seconds` and
  `tokens_per_second` (the serving pseudo-agent `local_vs_api`); and `bert_elapsed_ms` (the `intake`
  BERT fast path). They and the tier-1 metrics keep their names, definitions, units and
  aggregation, so a number means the same before and after cutover; the old registry stated no
  direction, so the table states one per row, proposed for the owner to confirm (D2 plan). What the master design drops is
  the machinery around them (tiers as a filter API, bundles, metric profiles), not the metrics. The
  new form is one flat, versioned metric table in configuration: each row is a metric id, its
  definition, unit, direction, aggregation, the layer and agents that produce it, and a `headline`
  flag (every old tier-0 id), so the dashboard view is a query over the table, not a tier system.
  `default_view`: each live agent (the 16 of llm-mailroom's taxonomy, listed in the table's
  `agent_roster`) has exactly one entry, a metric with a guard beside it and the label layers its
  gold comes from, or `null` with a reason; legacy auditors and retired specialists keep their
  rows for golden parity but get no default view (D2 plan,
  `docs/plans/2026-11-18-dojo-d2-measures-plan.md`, which holds the full table). **Owner
  decisions (2026-11-18, section 10):** `sorter` is judged on `f1_macro` ("the universal
  classifier headline", registry line 308), with `accuracy` as its guard; `boss` on wrong
  approvals (`boss_wrongful_approve_rate_vs_gold`), not `extraction_overall_score`, which grades the
  specialist; `intake` only on what is absolutely necessary, its triage class against the gold
  class, in its own planned row `intake_class_accuracy` with no guard (the shared ids keep their
  old meaning: in the old intake suite `accuracy` is the exact match of the cleaned text and
  `f1_macro` the token F1, `pipeline/intake.py` `_score_one`; the clerk diagnostics such as
  `intake_prep_completeness` are `not_ported`); the transcribers (`pdf_transcriber`,
  `image_extractor`) are disregarded for this step, a `null` view with that reason; and
  `human_review` has none, because people are the gold source. **Proposed, owner to confirm**
  (section 10): `sorter_reviewer` `reviewer_class_accuracy_vs_gold` (the tier-0
  `reviewer_class_accuracy` on derived gold); `judge` `judge_verdict_accuracy_vs_gold`, not
  `f1_macro` (the judge rules on extractions and has no class gold in the new system); `arbiter`
  `arbiter_decision_accuracy_vs_gold`; the specialists
  `extraction_overall_score` ("Specialist headline", line 326), except `extraction_f2` for the
  insurance claims specialist (line 344); `local_vs_api` `tokens_per_second` (tier 0, lines 1097-1098;
  `ttft_seconds` stays a headline row); procedural agents, graph nodes and agents with no metric
  yet have a `null` view with a reason. Gold is the dataset ground truth (`hub`), the generated
  interim and near-gold labels (`interim`, `near_gold`), the review desk's gold (`review`) and
  further gold generated for evaluation (a planned paid run behind the spend gate); label
  precedence is unchanged. The decision agents (sorter reviewer, judge, arbiter, boss) read
  `derived_gold`, computed from those layers' class and field labels. Derived gold is a new
  rule, so it lives in new design rows named `<old id>_vs_gold` that state it, and the old ids
  keep their old rule (decision 4 of section 10).
  Every default-view and guard metric names its pairing, or a note says why it has none. The
  serving ids are planned for the run layer's serving figures (D5; spec line 285).
  `bert_elapsed_ms` is `not_ported`: the BERT intake path is dropped (section 7) and nothing else
  reads it. Improvements: every value is a `Measure` (below); each compared metric names its
  pairing rule for comparisons, or why it has none; the noise floor column is null on every row
  until D7 measures it from repeat runs; per-class breakdowns are standard; new layers (behaviour, stability, cost truth, log
  health) add rows, never a parallel registry. Parity: every metric id the old registry and the
  golden suites emit is in the table with the same meaning, or listed as an intended change with a
  reason.
- **Which rows are implemented (one scope, used in sections 4.2, 6 and 10).** The table keeps a row
  for every old registry id, for parity accounting. Only these are implemented or planned: the
  headline metrics that have a reader (all tier-0 ids except `bert_elapsed_ms`), the metrics the
  golden suites emit (less the intake clerk diagnostics the owner dropped, section 10), and ids
  with a named reader (a gate, a report or a projection); planned `design` rows add the numbers
  the master design needs that no old metric gives (D2 plan). Every other id
  has status `not_ported` with a reason, and can be revived later without changing its meaning. This
  includes tier-1 ids: each keeps its row and its meaning, but a tier-1 id with no reader and no
  golden suite emitting it is `not_ported`, not implemented. Per-field, per-class accuracy is a row,
  and the keep rule reads it (D7, R2). This is the master design's "a module survives only if a
  gate, a report or a projection reads its output", applied to metrics. Each row says which it is:
  `implemented` (a producer exists in the workspace, and a test checks that every `implemented`
  row names its producing layer and function), `planned` (with the chunk that will produce it) or
  `not_ported`. The D2 gate checks that the `implemented` and `planned` rows together are exactly
  this scope.
- **`Measure` replaces scorecard honesty.** The old 1,176-line `scorecard_honesty` module is
  imported by 11 modules. The master design replaces it with one rule: every number carries its
  sample size, cost basis and the status of the labels behind it. So D2 defines
  `Measure(value, n, ci, cost_basis, label_status, method)` and a flat metric table; layers return
  `Measure`s, and a report cannot print a bare float. Old fail-closed ground-truth behaviour is
  kept as `label_status="unverified"` with `value=None`, never a silent zero.
- **Golden parity is the D3b gate.** The old `tests/fixtures/golden/scores.json` (21 suites, 22
  task kinds) and the 89 sandbox document-scorer goldens are captured read-only from the old Dojo
  and must be reproduced exactly, the goldens **with the old Dojo's departures overlay applied**,
  as the old Dojo's own parity tests do (`tests/conftest.py`, `doc_scorer_goldens` and
  `archive_block_before`, at `23f8234`). The overlay is `tests/fixtures/doc_scorers/departures.json`
  (dojo 0.28.1): deliberate score changes since capture (goldens at dojo 0.26.0, archive blocks
  before 0.27.0), 35 `payload` cases over the 89 `goldens.json` cases and 65 `archive_block`
  cases over the 89 `archive_block_before.json` cases. The raw captured goldens are not the target; they predate
  0.28.0's intended changes. Intended differences go in `score-changelog.md` and
  `intended_differences.json`, as in chunk 1. One is known now: the `legalbench` task kind is
  not kept, because the master design drops LegalBench scoring; it is listed as an intended
  difference with that reason, so D3b reproduces all 21 suites (none of them is LegalBench) and
  the other 21 task kinds.
- **The Dojo never reads run folders.** It reads `mailroom_record` only. Sandbox and llm-mailroom
  runs reach it through the record importer (another thread). So D1 to D4 need no record work
  and start when their waits-for in section 6 are met; D5 to D7 wait for the stage-1 gate
  (chunk 0).
- **The keep rule obeys R2.** The sandbox rule (`autoresearch/keep.py`, `decide_keep`) already
  requires a paired gain above the noise floor, no guarded class losing more than
  `max(max_class_drop, that class's replicate swing)` of utility, no rise in wrong archives and a
  cost bound. R2 says a change that lowers per-field accuracy on any class is a regression even if
  utility rises (spec line 44), and its success metric asks for per-field accuracy **and
  coverage** at or above today's on every class (spec line 49). D7 adds that guard on both: a
  candidate is reverted when any scored field of any class loses accuracy, or loses coverage, by
  more than `max(field floor, that field's replicate swing in that class)` for that measure; the
  floors live in configuration, and the reverting reason names the class, the field and the
  measure. The sandbox rule lets `guard_classes` narrow the guarded set; the Dojo requires it to
  equal every scored class of the run (a narrower list is refused with a named error), so no class
  goes unguarded.
- **Behaviour covers the eight slots of R10.** Sorter reviewer, archive gate, judge replacement,
  judge check, boss check, arbiter veto, closed-field resolver and field review (spec line 121),
  plus the default archive checks that need no model. For each slot the Dojo reports the master
  design's list (spec line 123): per-slot accuracy and override rate, silent-wrong per 1,000, cost
  per decision and the risk-coverage curve with bounds; for slots the policy marks as able to
  reroute, it also reports overrides against the slot's per-slot cap and flags a breach. The
  archive gate's confidence also gets its ranking power (spec line 286).
- **Suites are profile data where they can be.** An agent suite is the agent's name, its task
  kind and its metrics; a new agent needs a profile entry, not a new class.

### 4.3 The engine (stage 3, after the go or no-go gate)

| Concern | Design |
| --- | --- |
| Policy file | YAML seeded from llm-mailroom `graph/route_spec.py` (it already declares every edge as data and validates it): nodes, edges with named conditions, the routing thresholds (spec line 400; each class's sorter thresholds stay in the client profile, spec line 58), the eight decision slots (off by default, each with its caps, including per-slot override caps for slots that can reroute), retry budgets. Hash stored on every run. One file both draws (`engine graph --policy`) and runs. |
| Conditions | A closed registry of named condition functions ported from `graph/routing.py`; the policy references them by name with parameters, never code. |
| Agents | Today's roles; one model client function keyed by agent name, behind `mailroom_common.models`; prompts loaded from `config/prompts/<set>/` with id and hash recorded per call. |
| Handoffs | Typed envelopes (sender, confidence, blob refs, request), each an event. Model text is untrusted and never crosses a handoff as free text. |
| Executor | LangGraph with its checkpointer off; the record is the only checkpoint; each model response recorded before the engine acts on it. |
| Replay client | Serves stored step outputs back to the graph; router replay must be identical on every stored document-run (the go or no-go time box). |
| Queue and workers | Content-hash claims (SQLite locally, Postgres in a deploy), leases with heartbeat, breaker per provider, dead-letter after N attempts. The spend cap check and the reservation of the call's cost are one atomic step against the record's running total, before dispatch: a reservation counts toward the cap while its call is in flight; a call known to be uncharged (refused before the provider accepted it) releases it; a call whose charge is unknown (a timeout or a lost response after sending) keeps it until the charge is reconciled; a completed call replaces it with the recorded cost; and a crashed worker's reservation is kept and reconciled on resume. |
| API | Upload and review-resolve keep their shape; review resolution writes a new version (review station). Trace exporters optional, fed from record events. |

**Legacy parity hazards** the engine plan must close first (from the inventory, checked against
llm-mailroom `src/` at `070663f` and the sandbox at `93f298c`):

- `langchain_agents/base_agent.py` at `070663f` (`augmented_system_prompt`, lines 146 to 161)
  appends three things to every agent's system prompt: the 28 skill files, the rendered tool descriptions from
  `toolkit.py`, and the outcome memory. Skills and tool descriptions are static text and go into
  the hashed prompt set (P1).
- **Outcome memory is on in `legacy`, scoped to one run and starting empty.**
  `langchain_agents/memory.py` has no enable flag: it is read into the system prompt
  (`base_agent.py` line 140, at `070663f`) and into the sorter and specialist retry prompts
  (`graph/build_graph.py` lines 970 and 2162, at `070663f`), and written on guardrail rejections
  (lines 916 and 2015, at `070663f`). Its scope today depends on the launch path: the ledger is
  `<MAILROOM_BASE_DIR>/memory/<agent>.jsonl` (or `MAILROOM_MEMORY_DIR`), and `recent_context`
  reads the last 5 rows for the agent and document type from whatever that file holds. The
  sandbox's deploy path gives each run its own base folder (`job/deploy_env.py` line 133, at
  `93f298c`), so memory starts empty per run; a local run defaults to the shared `./data`
  (`runtime.py` line 196, at `93f298c`), so memory leaks across runs. `legacy` takes the deploy path's behaviour: memory is per
  run and starts empty. Every ledger row it writes and every context text it reads is recorded
  in that run, so replay stays exact even when parallel workers interleave. Like every model
  input and output (data flow, section 3), the texts are stored as blobs, written first; the
  event carries only the blob hashes, the agent, the document type and the row count, never the
  text itself, so no document or model free text lands in an event payload. Cross-run
  memory, if wanted later, is derived from the record index and is an intended difference for
  the owner to sign off, as is switching memory off. For mock end-to-end parity (proof item 3
  below) the old side runs with `MAILROOM_MEMORY_DIR` set to a fresh empty folder per
  run, so both sides start from the same empty state.
- Most effective prompt text lives in code (`langchain_agents/prompts.py`, 3,198 lines), so the
  `deploy_v1` set is materialised to files and compared byte for byte with what the old code
  sends (P1, before the engine).
- **The scoring family is an intended difference.** llm-mailroom's graph adds `score_common` and
  one `score_<type>` node per scorer (`graph/scoring_nodes.py`, `add_scoring_nodes`, lines 166 to
  176, wired in by `graph/build_graph.py` lines 3887 to 3889, at `070663f`): seven of its 22 nodes score
  inside the run. The workspace scores only in the Dojo, from the record (section 7), so
  `legacy` has no scoring nodes. Router replay and the drawn-graph comparison exclude the
  scoring family, and the exclusion is listed as an intended difference with that reason; every
  other node and edge must match.

**Proof that `legacy` matches** (master design, "Proof that `legacy` matches" and the reviewer's
calls, spec lines 425 to 427 and 584 to 585):

1. The stage-1 gate (chunk 0) audits each stored folder and reports which routing inputs are
   present. Router replay covers what is present, on every stored document-run.
2. The stored runs predate #309, so its checks are proven by the mock fixture set and by **one
   new recorded run on the 0.30.0 tag with every routing input captured**, about 25 documents,
   started by the owner on the current stack and imported (a paid run, through the spend gate).
3. Mock end-to-end equal on the fixture set, with outcome memory empty at the start of each run on
   both sides; throughput at or above 249 documents a minute.
4. Every mismatch is listed and fixed or signed off by the owner. No tolerance band.
5. **Time box and fallback** (spec line 550): if router replay does not match within the engine's
   time box, the owner's fallback is approach 3, slimming the current pipeline in place; the
   record, labels, Dojo and project 4 work keep their value.

### 4.4 The launcher (the sandbox, stage 4)

The sandbox becomes `mailroom_launcher`, as the master design says. It keeps the run spec,
preflight, the spend gate with initials and caps, the price sheet check, the Modal and Compose
deploy helpers and the autoresearch driver. It hands its scoring (run metrics, archive gate and
causes, specialist and grid cards, serving parity, schema adherence, routing audit, autoresearch
scoring and keep rule) to the Dojo layers above, and its run-folder reader to the record importer.
It takes over the operator surfaces: the `sandbox` CLI capabilities, the TUI (based on
mailroom-reloaded plus every native sandbox feature) and the floor with recorded replay, all reading
the record. The-Mailroom's server, operator desk and web floor join it, with every Langfuse or
Phoenix trace source replaced by a record tail. Before the engine exists, every run is sealed or
imported, so the tail is a read of a finished run's events in order (the record index and `trace`,
chunk 0), which L3 and L4 build on. Live follow of a run still being written needs a live writer: it
is a record-package addition (the record work owns it) made together with E1, the first live writer,
and L7 wires it into the CLI, TUI and floor. The desk's users and credentials live in the desk's (or
the review station's) own store, never in the record index, which holds only what is in the log.
Section 8 lists the launcher chunks from the launcher inventory.

A backend is an adapter with one interface (`start(run_spec) -> run_id`, `fields_reported()`);
a field it cannot report is recorded as absent. Backends: OpenRouter, Modal with vLLM, Docker
Compose, local Ollama or vLLM. Nothing else changes with the backend.

### 4.5 Prompt optimization and the merger work (project 4)

The master design's project 4 (spec line 483) needs only the record, labels and Dojo, so it runs on
the current pipeline and does not wait for the engine. The one exception is P3b: merger parking is a
routing rule (spec line 351), so it is an experiment policy and waits for E3. Project 4's runs are
made on the current stack (until launcher L8 exists, by the sandbox's own autoresearch loop, started
by the owner when they spend money), imported through the record importer, and scored and decided by
the Dojo.

| Chunk | What it builds | Gate |
| --- | --- | --- |
| P1 prompt sets materialised | `config/prompts/deploy_v1/` (agent prompts, skills, tool descriptions, doctrine), `frozen_v1`, the catalog | rendered prompts byte-identical to what the old code sends, per agent and class, with an empty outcome memory; the reference is llm-mailroom `070663f`, re-checked against the 0.30.0 tag once X2 lands; each prompt names every field of its schema's `extracted` section and every allowed value of its closed vocabularies (the alignment check, spec line 372). The check also reports the `scored` fields a `legacy` prompt never asks for (such as `acting_body`, section 4.1) as **known gaps**, recorded with a reason in a gaps file as the old Dojo's `prompts/data/alignment_gaps.json` did (empty at `23f8234`, because it compared against `doc_classes` only), and fails on any change in either direction; it never edits a `legacy` prompt to close a gap (a closed gap is a new prompt set). The alignment check lives in the workspace `tests/` until the engine exists and moves into the engine's tests in E2, as spec line 372 says |
| P2 merger schema and scoring | The merger schema (21 scored fields, version 2, plus MAUD clause answers) as closed, MAUD-style questions with fixed option lists, as a **new schema version** beside the frozen one D3a converted (the frozen file, and the D3b goldens that read it, stay unchanged); per-question scoring in the Dojo (spec lines 348 to 349). Waits for D3b so it never edits a schema file the goldens still read, and for R-H (section 6) so every run it scores names the schema version it used | each question maps to exactly one ground-truth label or is listed as unscored; the question list and mapping checked against the Dojo's MAUD schema and the EDA labels (spec line 353); coverage and precision reported separately per question, with a test where doubling the answers with no precision gain shows coverage up and precision flat (the v1 case); the 12-question clean subset scored |
| P3a merger bar check (project 4) | A Dojo report that says whether merger extraction reaches the bar, from P2's per-question scores; it decides nothing and writes nothing the engine reads. The bar is the archive gate's document quality bar, the `quality` key of D6's `config/clients/<client>/bars.yaml` (taken from the sandbox's `config/production/bars.yaml` at `93f298c`, `quality: 0.90`), not a new number. Per-question scores map onto it per document, as the archive gate compares one document score with `quality` (sandbox `job/gate.py` line 234, at `93f298c`): a merger document's merger-question score (P2's document score over its labelled, scored questions) meets the bar when it is at or above `quality`; the report gives the share of merger documents that meet it, with bounds. The sandbox reports mergers but never gates them (`gated` in that file); P3a is a report, so that stays true. Waits for D6 (row 7), which creates `bars.yaml` | planted scores just under and just over the bar give "unmet" and "met"; the bar is read from `bars.yaml`, never written in code (a test changes the file and sees the verdict move); the report names its sample size and label status |
| P3b merger parking (waits for E3) | An experiment-policy option that parks mergers for review with the answers attached as a prefill while the bar is unmet (spec line 351). The bar status is a policy or run-spec value, set by a person or the autoresearch driver from P3a's report; routing never reads a Dojo report (section 3, data flow) | a test with the value "unmet" gives "park, with prefill"; with "met", "route as usual". It is an experiment policy, not `legacy`, which must match today |
| P4 prompt variants and the keep loop | Variants as overlays on a prompt set, one agent's prompt each; targets from the per-field miss table; specialists first, then oversight agents, sorter last | a variant becomes the default only when the D7 keep rule accepts it on the development pool (paired gain above the noise floor, no class worse, the R2 per-field guard), it holds its gain on at least two worker models from different providers (spec line 383), and its confidence is scored for how well it predicts its own field scores; slice-500 is never in a tuning draw (a test checks the draw). Transport to the current stack, as in P6: each variant is also a sandbox prompt file (`config/prompts/<file>.txt`, selected by a `PromptRef` with `source: local` in the sandbox's `job/spec.py`), added through a reviewed sandbox change, never patched from here; a variant's runs count only when the prompt sha256 its run recorded (`prompt.lock.json`, imported into the record) equals the hash of the workspace overlay, computed the way the sandbox computes it: the sha256 of the overlay's prompt text encoded as UTF-8 (`prompt_registry._sha` over `path.read_text(encoding="utf-8")` at `93f298c`, so Python's newline translation applies and a CRLF file hashes as LF); a test pins one overlay's hash against that function's output |
| P5 threshold refit | Confidence thresholds refit after a prompt change (spec line 373), with the D1 calibration and `confidence/` | the refit is stored as a new configuration version beside its calibration report: each class's sorter thresholds as a new client-profile version (`config/clients/<client>/`, spec line 58), routing thresholds as a new experiment-policy version under `config/policies/` (spec line 400; before E3 exists, the report carries the proposed routing values and E3 writes them into a policy); the Dojo flags a run whose prompt set is newer than the thresholds it used, which needs the client-profile hash on the run header: P5 waits for R-H (section 6) |
| P6 landing on the current shipping tree | Accepted prompts and the improved merger schema as a new prompt set and schema version (spec line 384; open question 6: both also land on the current tree) | the files the current tree takes are hash-equal to the workspace's; they land through that repository's own reviewed change, never patched from here |

**Set E** ("cheapest safe handling of mergers": cost per document, clean-subset MAUD answers
correct) is the experiment that picks the merger extraction method: per-question passes over the
relevant section, the decision-model slots, or one pass (spec line 350, "Set E, widened"). P2
gives it its scoring, P3a its bar check and P3b its parking arm. Its extraction arms can run on
the current stack; its routing arms, P3b among them, are experiment policies at cutover (section
6, last row).

## 5. Scale and reliability

The master design's load estimate holds: about 200,000 events and 200 MB a day at 10,000
documents; one SQLite file is enough; the limit is the provider's rate limit. This port adds no
broker, warehouse or cluster. Reliability comes from the record's write order and from the
engine's leases, breakers, dead-letter state and pre-call spend cap (master design, "Failure
handling"). The Dojo is offline and pure, so its only scale concern is re-scoring a large run on a
laptop: the run reader streams documents from sealed columns and the layers are per-document maps
plus small aggregations.

## 6. Build order and gates

The master design's order, refined into chunks. Each chunk is its own PR with its own plan or plan
section, test strategy additions, one to three adversarial rounds and a fresh pre-PR review.
Record (PR #2) and the shared leaves (PR #6) are merged to `main`.

| # | Chunk | Waits for | Gate it must show |
| --- | --- | --- | --- |
| R-H | **Run header version 2** (record follow-up, section 3): `client_profile_hash`, `engine_schema_version` and `engine_schema_hash` (the header's own `schema_version` stays the record format version) | - | tier 3 (record schema): the upgrader reads every version-1 header unchanged in meaning (property test over generated headers); a version-2 header without the new fields is refused; the importer fills them for an imported sandbox run from `spec.lock.json` and leaves `label_snapshot_ids` empty (section 3); one deliberate mutation turns a test red |
| 0 | **Stage-1 gate** (the record work, another thread: importer, replay timeline, pack and prune) | X1 | master design stage 1 (spec line 457): v1 and slice-100 r2 import cleanly, every chain verifies, `trace run` and `trace doc` work, stored size reported against today's; plus the routing-input audit of each stored folder (spec line 585) |
| 1 | D1 statistics | - | old tests and a differential corpus, identical |
| 2 | D2 headline registry metrics in one flat table, and `Measure` | - | a row for every old registry id and every metric `scores.json` emits, each with the same meaning (or an intended change with a reason) and a status: implemented, planned (with its chunk) or `not_ported` (with a reason); implemented or planned rows are exactly the scope in section 4.2, and a test checks that every `implemented` row names a producer; every old tier-0 id flagged `headline` (14, several per agent), and the dashboard queries leave `not_ported` rows out by default; every live agent has exactly one `default_view` (a metric with its guard and label layers, or `null` with a reason), the owner's decisions of section 10 marked confirmed and every other entry marked a proposal (section 4.2); every default-view and guard metric names its pairing or why it has none; provenance tests |
| 3 | D3a one schema source per class | 2 | each section equals its own old table, as frozen JSON dumps: the `scored` sections equal the old Dojo's `doc_classes` and per-class profile tables at `23f8234`; the `legacy` `extracted` sections equal llm-mailroom `EXTRACTION_SCHEMAS` at `070663f` (section 4.1); labels, Dojo (and later engine) load the same files |
| 4 | D3b tasks, suites, document scorers | 1 to 3 | `scores.json` (all 21 suites; 21 of 22 task kinds, `legalbench` an intended difference) and the 89 document-scorer goldens with the old Dojo's departures overlay (`tests/fixtures/doc_scorers/departures.json`, dojo 0.28.1) applied, reproduced exactly (section 4.2) |
| 5 | D4 label layer, Dojo joined to `mailroom_labels` | 3 | old label-policy cases; Protocols checked against the real `LabelSet` |
| 6 | D5 run reader and run layer | 0, 1, 2, 4, 5 | old tests; numbers on imported runs; a run is scored only against a label snapshot named explicitly (for a native run, one its header names; an imported run, whose `label_snapshot_ids` is empty, against none by default), and every report records that snapshot's id and manifest hash (section 3); class accuracy 100 of 100 on v1; per-field, per-class accuracy and coverage table (D4 scorability, D3b scorers); creates `config/prices/` (the price sheet, with its layout-map line) and cost figures read from it, never from code |
| 7 | D6 outcome and behaviour (archive gate, cause codes, wrong-archive and review rates, risk-coverage, routing audit, the eight slots, hand-off effect) | 4, 6 | old and sandbox tests; v1 outcome counts; wrong-archive and review rates with bounds; risk-coverage bounds checked against exact binomial bounds on planted data; per-slot reports on synthetic decision events with the R10 list (accuracy, override rate, silent-wrong per 1,000, cost per decision, risk-coverage with bounds), including a flagged cap breach; the archive gate's ranking power; creates `config/clients/<client>/bars.yaml` (with its layout-map line) equal to the sandbox's `config/production/bars.yaml` at `93f298c`, and the gate reads its bars from it |
| 8 | D7 comparison, keep rule, projections, stability, cost truth, log health | 6, 7 | planted-curve property tests; refusal rules; keep-rule tests, including a candidate with a utility gain reverted for one field's accuracy drop beyond noise, one reverted for one field's coverage drop beyond noise (R2), and a `guard_classes` narrower than the scored classes refused; cost truth prices tokens from D5's `config/prices/` |
| 9 | **Stage-2 gate** | 0 to 8, X1, X3 (Set A also X2) | goldens unchanged; v1 and slice-100 r2 reproduced (r2: 40 of 55 wrong archives and 9 of 95 reviews, spec line 323); Sets A and C scored |
| 10 | P1 prompt sets | 3 | section 4.5 |
| 11 | P2 merger schema and scoring | 3, 4, 5, R-H | section 4.5 |
| 12 | P3a merger bar check | 7, 11 | section 4.5 |
| 13 | P4 variants and keep loop | 8, 10 | section 4.5 |
| 14 | P5 threshold refit | 13, R-H | section 4.5 |
| 15 | P6 landing on the shipping tree | 11, 13, 14 | section 4.5 |
| 16 | **Go or no-go** (owner) | 9 | the owner builds the engine or takes approach 3 (spec line 550) |
| 17 | Routing capture run: one new recorded run on the 0.30.0 tag with every routing input captured, about 25 documents, **started by the owner** (spec line 584) | 0, X2 | imports cleanly and every chain verifies; it covers the routing inputs chunk 0's audit found missing |
| 18 to 23 | E1 foundation (with the record's live follow, section 4.4), E2 agents and handoffs (the prompt alignment check moves in), E3 policy and compiler and `legacy`, E4 replay, E5 queue and workers, E6 API and exporters | 16 (E2 also 3, 10 and X2; E3 also X2; E4 also 17) | router replay identical on every stored document-run for the inputs present and on the capture run for the rest (without X1, the stored runs are v1 and v2 only: r2's 95 document-runs stay unverified and the narrowed gate needs the owner's sign-off, as for rows 0 and 9), excluding the scoring family as an intended difference (section 4.3), time-boxed with the approach-3 fallback; mock end-to-end equal; at least 249 documents a minute; a mock upload through the E6 API writes a record that verifies; concurrent workers offered more mock paid calls than the cap allows never dispatch more than the cap (E5) |
| P3b | P3b merger parking (section 4.5) | E3 (rows 18 to 23), 12 | the park and route tests: with the bar status "unmet", a merger is parked for review with its answers attached as a prefill; with "met", it routes as usual; the status is read from the policy or run spec, never from a Dojo report (a test with no report present); `legacy` routes unchanged (router replay) |
| 24 | Launcher before the engine: L1, L6, L3, L4 (section 8) | L1: 3; L6: L1, 6 (D5 creates `config/prices/`); L3: 0, L1, L6; L4: 0, L3 | as in section 8 |
| 25 | Launcher after the engine: L2, L7, L8, L9, then L5 with the review station (section 8) | 23, 24 | a mock run per backend with no spend; paid parity runs started by the owner |
| 26 | Cutover: experiment policies B, D, E (Set E's routing arms include P3b, merger parking); Set F | all | Set F pass rule (approved 2026-11-17) |

External dependencies, outside this workspace:

- **X1, slice-100 r2 folder supplied (owner).** Master design open question 1 (spec line 593):
  the folder is still missing, and the record plan imports it "when its folder is found". If it
  never turns up, rows 0 and 9 are shown on v1 (and the v2 runs, spec line 593) only; the r2
  numbers (spec line 323) and r2's 95 document-runs for router replay (spec line 425) stay
  unverified and are listed as such in both gate reports, and passing a gate without r2 is a
  narrowed gate that needs the owner's sign-off. Nothing passes on r2 silently.
- **X2, the `legacy` tag exists.** llm-mailroom #309 and #310 merged and the 0.30.0 tag cut
  (spec line 583); the capture run is made on that tag, and E2 and E3 check `legacy` against it.
  Not met at `070663f`: llm-mailroom has no 0.30.0 tag yet.
- **X3, Set A and C runs exist.** Set A and C runs made on the current stack by the owner (paid,
  owner-started, through the sandbox's spend gate) and imported through the record importer.
  The master design lets them start on the current stack (spec lines 146 and 474) and has stage 2
  score them (spec line 458), so row 9 waits for X3. Set A also waits for X2, so that its
  sorter-review runs are made on the `legacy` reference (the 0.30.0 tag) that the engine is
  later checked against. No agent starts these runs.

Rows 1 to 5 and 10 need no record work and start when their waits-for are met; 1 and 2 start
now (11, P2, follows 4 and 5 and also waits for the record change R-H). **Approved deviation (owner,
2026-11-18, decision 5):** the master design builds the engine (project 5) before the launcher
(project 6). Here the launcher chunks that need no engine (row 24: run spec and preflight, the
spend gate and price sheet, the CLI over recorded runs, the floor) go ahead before the go or
no-go gate and work over imported runs only; the run driver and everything that starts a run
wait for the engine.

### 6.1 Test strategies per package (R8)

R8 asks later packages to get the record's treatment in their own strategy (spec line 87). Each
follows `docs/test-strategy-record.md` (principles, a claim-to-test table, a fault matrix,
mutation checks):

- **Dojo:** `docs/test-strategy-dojo-core.md` exists for chunk 1; D1 to D7 and P2 add their
  sections (differential corpora, planted-curve properties, refusal rules, the keep-rule guards).
- **Engine:** `docs/test-strategy-engine.md`, written in E1 (killed workers, lease expiry, breaker
  states, spend cap at the boundary, replay mismatches, hostile model text across a handoff).
- **Launcher:** `docs/test-strategy-launcher.md`, written in L1 (the preflight and spend-gate
  fault matrices, `kill -9` resume, timeline differential tests, CLI surface check).
- **Labels, common and dictionaries:** no strategy document exists yet (only
  `docs/test-strategy-record.md` and `docs/test-strategy-dojo-core.md` do), and their package
  READMEs carry none. The next PR that changes each package writes
  `docs/test-strategy-<package>.md` in the record's shape, starting from the tests it already has:
  labels (snapshot and manifest hashing, precedence and the quality gate, importers, the real
  snapshots), common (hashing, JSONL and YAML round trips, retry, redacting logs) and
  dictionaries (versioned vocabularies, the lock, the glossary). The README's layout map says
  which strategies exist today.

## 7. What is not ported

| Old | Why |
| --- | --- |
| llm-mailroom `audit/`, `storage/`, `result_cache`, `bins`, audit scripts (10,676 lines) | replaced by the record |
| Scoring nodes in the graph, `observability/*` Dojo adapters | the Dojo scores from the record |
| LegalBench, `dojo_source`, `bert_intake`, Langfuse/Braintrust/Phoenix setup and sync, release and pin scripts | dropped by the design or obsolete in one workspace |
| The-Mailroom trace sources and `trace_interpreter`, vendored mirrors | replaced by the record and by config |
| Old Dojo `config.py` globals, `scorecard_honesty`, metric tiers | replaced by profiles, `Measure` and Dojo reports |
| Sandbox study-specific report modules | retired by the master design (spec line 439); their tables return only as Dojo reports rebuilt from records |

The llm-mailroom DROP total in the inventory (63 modules, 13,097 lines) covers the second and
third rows together.

### Capabilities outside the master design's text: proposed fate, for the owner to confirm

The master design does not name these. Each has a proposed fate; none is final until the owner
confirms it (section 10). Sources are the two inventories' OPEN and DROP rows.

| Capability today | Source | Proposed | Form or reason |
| --- | --- | --- | --- |
| Gmail intake (`pipeline/gmail_intake.py` 2,722 lines, mail guards, outbox, smoke test) | llm-mailroom; reloaded `intake/` | engine | an intake channel adapter beside folder and HTTP upload, feeding the same content-hash queue |
| SMS pager, watchdog, status notify, ops monitor, pager provider health | llm-mailroom `pipeline/`; reloaded `obs/pager*`, `obs/sms.py`, `llm/health.py` | launcher | an operations module that tails the record and the queue; notification transports are adapters, credentials only in env vars |
| Vision classifier (`langchain_agents/classifier.py`) | llm-mailroom | engine | an agent registered for image documents, model chosen by the run spec |
| Outcome memory (`langchain_agents/memory.py`) | llm-mailroom | engine | on in `legacy` (it is always on today), per run and starting empty as on the sandbox's deploy path, its inputs and outputs recorded as events (section 4.3); cross-run memory would be derived from the record index |
| Agent tool registry (`langchain_agents/toolkit.py`) | llm-mailroom | prompt set, then retire | its rendered descriptions are prompt text in `legacy` and go into `deploy_v1` (P1); the registry retires unless the engine gives agents tools |
| Regex post-hoc ground truth (`posthoc_gt.py`, `extraction_gt.py`) | llm-mailroom | Dojo | a diagnostic report, never a label layer (R6 precedence is unchanged) |
| LLM-as-judge evaluators over a run (`scripts/run_quality_judges.py`) | llm-mailroom | retire | the judge is an engine agent scored by the behaviour layer; a separate judging pass would be an unrecorded model call |
| Relations scanner CLI (`pipeline/relations_scan.py`) | llm-mailroom | engine | a command beside the relations agent |
| Relations graphs (`pipeline/relations_graph.py`) | llm-mailroom | retire | matplotlib and plotly figures that no gate or report reads |
| Hugging Face Space publishing and probing (`publish_space.py`, `probe_hosted_spaces.py`, the Space payload) | llm-mailroom, The-Mailroom | open | hosted writes; if kept, a launcher command the owner runs, never a test or CI step |
| Hosted Observatory (`hosted/`, `server/hosted.py`) | The-Mailroom | launcher | a read-only floor view over a sealed record export with no document text |
| React desk UI (`ui/`) | The-Mailroom | review station (other thread) or L5 | joins the review station's UI plan, or L5 if that UI does not cover it |
| The-Mailroom visualizer | The-Mailroom | launcher floor | reads the record; replaced at cutover (owner decision 3) |
| `tools/pipeline-diagnostics` (inspect and check over old run folders and bins) | llm-mailroom | retire | the record's `trace` and `verify` replace it; open only if the owner wants its workbench UI |
| Braintrust and Phoenix trace sinks | llm-mailroom `audit/sinks/`, `observability/` | retire | the master design names Langfuse and OpenTelemetry exporters only; Phoenix can read the OpenTelemetry export |
| Free-model fallback (`free_model_swarm` in `taxonomy.yaml`) | llm-mailroom | open | if kept, a fallback list in the client profile, with no provider name in code |
| Extraction status page (`build_extraction_status.py`) | old Dojo | open | the labels coverage report may cover it |
| Static terminal (`terminal/`) and constellation repo browser (`tui/repos.py`) | The-Mailroom | retire | the launcher TUI is the terminal; one workspace has no repositories to browse |
| Jev learned route gate (`agents/jev.py`, `agents/gate.py`, `eval/train_gate.py`, `eval/jev_calibration.py`, `scripts/jev_harvest.py`, `jev_dev_rows.py`, `jev_export_gate_features.py`, `deploy/mock_jev.py`) | reloaded | open | a decision model (R10): if kept, it returns as a decision-slot experiment arm (policy and config) scored by the behaviour layer, not as a separate gate |
| Behavioural conformance suite per model role (`eval/conformance.py`) | reloaded | open | an engine E2 test suite idea |
| External anchor for a ledger head (`storage/anchor.py`) | reloaded | open | the record work decides |
| CrewAI pipeline (most of the 67 retired modules) | reloaded | retire | a second engine; its ideas (two-call path, deterministic gate) can return as policies |
| `runbook` command and `config/runbooks/` | sandbox | open | keep the reader if runbooks stay; the study runbooks retire |
| macOS autostart plist for `sandbox board` | sandbox | open | operator convenience, not in the design |
| HTCondor batch backend (`deploy/htcondor/`) | sandbox | open | not one of the master design's four backends |
| Model-labelled ground truth (`gt_labeler.py`, `deploy/modal_gt_labeler.py`) and the sorter hand-off baseline (`export_sorter_handoff.py`, `patch_sorter_handoff.py`) | sandbox | labels, open | labels project; model calls need the owner's go-ahead |

## 8. Launcher chunks

The launcher is the sandbox, rebuilt as `packages/launcher` (owner, 2026-11-18). The module-by-module
inventory of the sandbox (`93f298c`) and of mailroom-reloaded (`2b79c28`, the TUI base) is
`docs/plans/2026-11-18-launcher-inventory.md`. Of the sandbox's 207 modules (79,218 lines), 81
(34,807) become launcher code, 27 (9,030) move to the Dojo, 11 become configuration, 9 go to labels,
7 to the record and 5 to the engine; 2 (3,485) are split between packages and 1 (295) goes to the
review station; 64 (18,677), mostly study-specific reports and vendor plumbing, retire, and their
tables come back only as Dojo reports rebuilt from records. Of mailroom-reloaded's 104 Python
modules (29,470 lines), the launcher keeps 9 (the ready route, replay sessions, the replay view
model, dev fakes and seed scripts), the browser TUI (no npm) and parts of 2 split modules (the CLI
and the API app); 9 go to the record; 1 (253) goes to the review station; 16 (6,188 lines: the Jev
route gate, the conformance suite, the ledger anchor, Gmail intake and the SMS pager with its
provider health) are open for the owner in section 7; 67, mostly the CrewAI pipeline, retire.

Rows are in build order; L5's position is pending the owner (section 10). "Engine" says whether the
chunk waits for the engine (owner decision 5).

| Chunk | What it ports | Depends on | Engine | Proof gate |
|:---|:---|:---|:---|:---|
| L1 run spec, draws, preflight | `job/spec.py`, `job/preflight.py`, draws and dataset locks; adds the policy hash and run kind the spec lacks | record header (on `main`), labels snapshot ids, client profile (D3a) | no | spec round-trip and hash property tests; every sandbox run YAML converts or is listed with a reason; preflight fault matrix, one exact refusal per fault |
| L6 spend gate, caps, price sheet | `job/spend.py` (initials, 24 h receipt bound to a fingerprint), budgets, ledger, price sheet `--check` over `config/prices/` (created by D5; L6 adds the sandbox's `config/api_prices.yaml` entries) | L1, Dojo D5 (`config/prices/`) | no | no paid action without a fresh receipt: each refusal path is a test; the signer lands on the Run |
| L3 CLI and TUI | sandbox CLI and TUI with the reloaded browser TUI as the base, over recorded runs | record index and trace (chunk 0; the tail over sealed and imported runs, section 4.4); L1, L6 | no | every sandbox command maps to a new one or a listed retirement (section 10); the reloaded JS tests green; run-start commands arrive with L7 |
| L4 floor | sandbox floor server and digest, reloaded replay sessions | record replay timeline (chunk 0), L3 | no | the record timeline equals sandbox `build_timeline` on the same folder (differential test); it replaces The-Mailroom at cutover |
| L2 backends | OpenRouter, Modal with vLLM, Compose (the producer image and the local serving services), local | L1, L6, engine E1 fake model and E6 API | yes | stage-4 gate, part 1: one mock run per backend with no spend, each writing a record that verifies |
| L7 run driver | `job/runner.py`, eval runners | L1, L2, record writer, engine E2/E3 | yes | `kill -9` mid-run resumes without repeating a recorded call; a call in flight at the crash may be repeated once, the exposure the master design accepts (spec, the Resume row of "Changes this review makes"); pause and cancel |
| L8 experiments and autoresearch driver | `autoresearch/loop.py`, levers, estimates, experiment matrix | L1, L6, L7; Dojo D6 utility and D7 keep rule | yes | the mock study runs at zero spend; re-deciding the stored rounds reproduces the old keep and revert decisions except where the R2 per-field guard reverts them, each such case listed |
| L9 stage-4 gate | `scripts/smoke_campaign.py` | L1 to L8 | yes | mock run per backend, then one paid parity run per paid backend, started by the owner through the spend gate |
| L5 operator desk | The-Mailroom operator desk; users and credentials in its own store | L4, review station | scheduled after L9 (section 6, row 25); it needs no engine code, so the owner may move it before the engine (not named in decision 5; position pending the owner) | its own gate: (1) JWT auth refusal paths, each a test with the exact refusal: missing token, expired token, wrong role (The-Mailroom `operator_desk/auth.py`, at `40a0600`, refuses the first two with 401 and a role below `require_role` with 403, line 266); (2) no document text in desk logs: a log-scan test runs the desk over synthetic documents carrying a marker string and finds the marker in no log line; (3) the desk's store (users, credentials, sessions) is kept apart from the record index: the boundary test finds no desk table in the index and no import of the desk store from `mailroom_record`; (4) archive file access is read-only: the PDF is opened with `fitz` from the archive blob, and a test shows the blob's hash unchanged after every desk read |

The sandbox's scoring code goes to the Dojo chunks it belongs to: D3b (the reference caller of the
document scorers), D4 (scorability and the labelled count), D5 (run metrics, cards, serving parity,
schema adherence), D6 (archive gate, cause codes, routing audit, document utility) and D7 (keep rule,
paired delta, cost truth, comparisons). None of them may import launcher modules.

Findings that shape the chunks:

- **Before the engine exists** (owner, 2026-11-18, decision 5, an approved deviation from the
  master design's order): the run driver L7 waits for the engine. The sandbox drives the pipeline
  today by importing a vendored llm-mailroom, which the workspace forbids, and no temporary legacy
  driver is built. Until the engine lands, L1, L6, the CLI over recorded runs (L3) and the floor
  (L4) go ahead, and the launcher works over imported runs only.
- **Two replay contracts.** The reloaded viewer reads a richer `replay/v1` payload (stations, segments,
  generations, scores, rollups) than the sandbox `build_timeline`. The record timeline carries what
  `replay/v1` needs, or the floor computes it from record events. This goes to the record work.
- The sandbox node status `error` maps to R7's `failed` in the importer and the timeline.
- The keep rule at `93f298c` (`autoresearch/keep.py`, `decide_keep`) already limits a class's
  utility drop by that class's replicate noise, which answers the master design's "per-class guard
  rewritten to respect per-class noise". It lacks R2's per-field guard; D7 adds it (section 4.2).
- One `modal` pin for the workspace (the sandbox has 1.6.3, reloaded 1.5.5); Node in the CircleCI
  image for the TUI's JS tests (a governance change); layout-map lines for the new `config/` folders
  and launcher subpackages before they are created; any run ledger is a generated view of the record.

## 9. Trade-offs

| Choice | Gives | Costs | Revisit when |
| --- | --- | --- | --- |
| Schema files as the contract, three small loaders | No shared package, each package installable alone | Three loaders to keep in step (a workspace test checks them) | A fourth reader appears |
| `Measure` instead of an honesty module | One rule, impossible to print a bare number | Every layer returns a richer type | Reports become slow to build |
| Golden parity before new metrics | Old numbers stay comparable across the cutover | New layers wait for D3b | The old Dojo is retired |
| Launcher work before the go or no-go gate | Operator surfaces over imported runs early | Nothing in the workspace starts a run until the engine exists | The engine is built |
| Port by verdict, not by file | About a third of the code never moves | Each drop needs a reason in the inventory and the owner's confirmation | A dropped capability is asked for |

## 10. Owner decisions

Decided 2026-11-18:

1. Keep the `common` and `dictionaries` leaf packages (section 3).
2. Adopt the ruff, mypy and codespell gates (section 3).
3. The launcher floor replaces The-Mailroom at cutover; the old visualizer stays on its pin, so
   the trace exporter needs no span-name compatibility (answers master design open question 2).
4. The headline registry metrics stay, improved on the new system, with consistent meanings:
   every kept id keeps its old meaning, and a new meaning gets a new row. Headline means the old
   tier 0 (14 ids, several per agent). The metric table keeps a row for every old registry id;
   only headline metrics with a reader, golden-emitted metrics and ids with a named reader are
   kept (implemented or planned), and the rest (tier 1 included, and `bert_elapsed_ms`) are
   `not_ported` with a reason (section 4.2).
5. The run driver waits for the engine; no legacy driver for llm-mailroom 0.30. The run spec and
   preflight (L1), the spend gate (L6), the CLI over recorded runs (L3) and the floor (L4) go
   ahead before the engine (section 8). This deviates from the master design's project order
   (engine, project 5, before launcher, project 6).
6. Per-agent default views and gold (section 4.2): the sorter is judged on `f1_macro`; the boss
   on wrong approvals; intake is simplified to what is absolutely necessary, the accuracy of its
   triage class against the gold class (its own row, `intake_class_accuracy`), with no guard,
   and the clerk diagnostics dropped; the transcriber roles are disregarded for this step. Gold
   is the dataset ground truth, the Claude interim and near-gold labels already generated, the
   review desk's gold, and further gold generated for evaluation (a planned paid run behind the
   spend gate); it never waits for human resolutions, the decision agents' gold is derived from
   those layers (D2 plan), and label precedence does not change.

Decided 2026-11-19:

7. scipy and numpy are core dependencies of the Dojo, not an optional extra (was open question
   T00). This departs from the master design's line that the Dojo's core dependencies stay small,
   with numerical fitting behind an optional extra; the floors are in `packages/dojo/pyproject.toml`.

Still open, for the owner to confirm:

- The proposed fate of each capability in section 7's second table, including the Jev learned
  route gate (a decision model, relevant to R10), Gmail intake, the SMS pager, Hugging Face Space
  publishing, the free-model fallback, `tools/pipeline-diagnostics` and the Braintrust and Phoenix
  sinks.
- Outcome memory in `legacy`: on, per run, starting empty, as on the sandbox's deploy path
  (section 4.3). Cross-run memory (derived from the record index) or switching it off is an
  intended difference.
- The D2 default views marked as proposals (section 4.2: `sorter_reviewer`, `judge`, `arbiter`,
  the specialists, `local_vs_api`, and the `null` entries with a reason) and the proposed metric
  directions (D2 plan).
- If the slice-100 r2 folder is never supplied: sign off rows 0 and 9 narrowed to v1 and v2
  (section 6, X1).
- R7 retirements, the native sandbox and reloaded features that do not move: the sandbox
  `subagents` group (with `preflight`, `dispatch-check`, `checkpoint`), `api` (`status`, `eval`),
  `run export-bt`, `fetch-deps`, the snapshots group, `modernbert` and `traces` (`export`, `pack`,
  replaced by the record's trace and pack); the `runbook` group (open); from reloaded, the `jev`
  view and the Phoenix and Grafana outbound links.
- L5 operator desk: scheduled after L9 (section 6, row 25, and section 8); it needs no engine
  code, so it can move before the engine if the owner extends decision 5.

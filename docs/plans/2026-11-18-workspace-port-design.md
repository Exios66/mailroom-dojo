# Workspace port: system design for the remaining packages

Date: 2026-11-18. Status: proposed, for the owner's review. Serves the master design
`docs/specs/2026-11-18-lean-rewrite-design.md` (R1 to R10, "Build order and proof gates",
"Systems review"). Where this document and the master design disagree, the master design wins
and this document is wrong.

The module-by-module inventory behind every number here is
`docs/plans/2026-11-18-workspace-port-inventory.md` (old Dojo, llm-mailroom, The-Mailroom). The
sandbox and mailroom-reloaded inventory follows in a later commit.

## 1. Scope

What is left to move into this workspace, after record (project 1), labels (project 2) and the
Dojo scoring core (project 3, chunk 1):

| Source | Physical lines (tests excluded) | Destination |
| --- | ---: | --- |
| `llm-dojo-scoring` 0.30 (old Dojo) | 31,450, of which 5,385 already ported | `packages/dojo`, `config/clients/<client>/` |
| `llm-mailroom` `src/` (pipeline) | 80,124 | `packages/engine`, `config/policies/`, `config/prompts/`, some Dojo, some launcher |
| `local-mailroom-sandbox` (**the launcher**, as the design says) | see the launcher inventory | `packages/launcher`, some Dojo, the record importer |
| `mailroom-reloaded` (TUI base, R7) | see the launcher inventory | `packages/launcher` (TUI) |
| `The-Mailroom` (floor, operator desk, server) | 15,926 Python plus about 11,100 front-end | `packages/launcher` (operator surfaces) |

Out of scope here, owned by other threads: the human review station (desk plan and ADR-0007 on
its own branch) and the run log storage (the record's importer, floor replay timeline, pack and
prune: record plan Tasks 10 to 12). This document names where they plug in and nothing more.

### Principle: one workspace for all functionality, rebuilt better

mailroom-dojo is the one workspace for **every capability** the constellation repositories
provide today (old Dojo, llm-mailroom, the sandbox, mailroom-reloaded, The-Mailroom),
refactored and improved, not copied. Two rules from the master design decide the form each
capability takes:

1. **Every capability lands here.** If an operator, a client or an experiment uses it today, it
   has a home in exactly one package (or in `config/`), behind the one-way boundaries. Nothing
   is left behind in a constellation repo that the new stack needs. The old repositories stay
   readable on their pins until cutover (master design, non-goals) and then take fixes only.
2. **Code survives only if something reads its output** (master design, "What is dropped" and
   the "second bloat" anti-goal). A capability is kept; the old module that implemented it is
   kept only when it is the best way to provide it. So most old code is replaced: storage and
   audit by the record, trace readers by the record, Python tables by configuration, eleven
   honesty call sites by one `Measure` type, a 22-node graph module by a policy file.

The only capabilities that do not move are the ones the master design drops by name: LegalBench
scoring, the metric registry's tiers, bundles and profiles, the legacy flat import paths, the
release-awareness tool, and the mailroom-ml label contract (it stays on the 0.30 line with its
user). Lines are a size estimate, not a target; about a third of the old lines are replaced, not
ported (section 7).

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

Data flow for one run: the launcher resolves a run spec (client profile hash, policy hash, prompt
set and overlays with hashes, label snapshot manifest hash, draw, backend, caps) and hands it to
the engine; the engine writes every step, handoff, decision and model response to the record
(blob first, then the event, then the index); the Dojo later reads the sealed record plus the
label snapshot named in the run and writes reports. Nothing flows back from the Dojo to the engine
except through a human or the autoresearch driver choosing the next policy.

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
├── schemas/<class>.yaml   the ONE field list: name, type (Dojo field types), vocabulary ref,
│                          coverage rule, containment, accepted-unscored, variants (e.g. the CMS row variant)
└── scoring.yaml        Dojo-only scoring rules (bands, audits, trace capture, list matching),
                        no field lists
```

Labels, Dojo and engine each read `schemas/<class>.yaml` through their own small loader (no shared
loader package needed; the file format is the contract, versioned with `schema_version`, and a
workspace test loads it with all three). Old Python tables (`cuad_schema`, `maud_schema`,
`corporate_record_schema`, `extraction_schemas`, `doc_classes`) become these files; an equality
test compares the loaded YAML with a frozen JSON dump of the old tables, as chunk 1 did for field
types. This is Dojo chunk D3a, and it unblocks the engine's specialists and the prompt sets.

### 4.2 The Dojo's remaining layers

The master design's "What it scores" table is the module layout. Each layer is a pure function
from typed inputs to `Measure` values; the record reader is the only place that touches storage.

```
mailroom_dojo/
├── profile/, values/, normalization/, field_scoring/, classification/, labels_interface/   (done)
├── statistics/        bootstrap, calibration (ECE, ranking power), paired tests        D1
├── measures/          Measure value type + the flat metric table loader (headline and
│                      supporting metrics; every number carries n, cost basis, label status) D2
├── tasks/, suites/    task kinds and per-agent suites, document scorers                 D3b
├── label_layer/       scorable-field rules over mailroom_labels.LabelSet                D4
├── run_reader/        typed view of a sealed record (documents, steps, decisions, costs) D5
├── run_layer/         classification, rates with bounds, cost per agent/class/outcome,
│                      schema adherence, serving by backend                              D5
├── outcome/, behaviour/   archive gate and cause codes, per-slot summaries, overrides    D6
├── compare/, projections/, stability/, cost_truth/, log_health/                         D7
└── reports/           report builders (registered), written by scripts under docs/reports/
```

Key decisions:

- **The headline registry metrics stay, improved (owner, 2026-11-18).** The old registry's
  headline metrics (tier 0: one board-level number per agent, such as macro F1 for the sorter and
  the overall extraction score for each specialist) and the tier-1 metrics under them keep their
  names, definitions, units, direction and aggregation, so a number means the same before and
  after cutover. What the master design drops is the machinery around them (tiers as a filter
  API, bundles, metric profiles), not the metrics. The new form is one flat, versioned metric
  table in configuration: each row is a metric id, its definition, unit, direction,
  aggregation, the layer and agents that produce it, and a `headline` flag (the old tier 0), so
  the dashboard view is a query over the table, not a tier system. Each live agent (the 16 of
  llm-mailroom's taxonomy) has one `default_view` with a guard and the label layers its gold
  comes from, or `null` with a reason; legacy auditors and retired specialists keep their rows
  for golden parity but get no default (D2 plan, `docs/plans/2026-11-18-dojo-d2-measures-plan.md`). Improvements: every value is
  a `Measure` (below); each metric names its noise floor and its pairing rule for comparisons;
  per-class breakdowns are standard; new layers (behaviour, stability, cost truth, log health)
  add rows, never a parallel registry. Parity: every metric id the old registry and the golden
  suites emit is in the table with the same meaning, or listed as an intended change with a
  reason.
- **`Measure` replaces scorecard honesty.** The old 1,176-line `scorecard_honesty` module is
  imported by 11 modules. The master design replaces it with one rule: every number carries its
  sample size, cost basis and the status of the labels behind it. So D2 defines
  `Measure(value, n, ci, cost_basis, label_status, method)` and a flat metric table; layers return
  `Measure`s, and a report cannot print a bare float. Old fail-closed ground-truth behaviour is
  kept as `label_status="unverified"` with `value=None`, never a silent zero.
- **Golden parity is the D3b gate.** The old `tests/fixtures/golden/scores.json` (21 suites, 22
  task kinds) and the 89 sandbox document-scorer goldens are captured read-only from the old Dojo
  and must be reproduced exactly. Intended differences go in `score-changelog.md` and
  `intended_differences.json`, as in chunk 1.
- **The Dojo never reads run folders.** It reads `mailroom_record` only. Sandbox and llm-mailroom
  runs reach it through the record importer (another thread). So D1 to D4 start now; D5 to D7 wait
  for the importer.
- **Suites are profile data where they can be.** An agent suite is the agent's name, its task
  kind and its metrics; a new agent needs a profile entry, not a new class.

### 4.3 The engine (stage 3, after the go or no-go gate)

| Concern | Design |
| --- | --- |
| Policy file | YAML seeded from llm-mailroom `graph/route_spec.py` (it already declares every edge as data and validates it): nodes, edges with named conditions, thresholds, decision slots (off by default, with caps), retry budgets. Hash stored on every run. One file both draws (`engine graph --policy`) and runs. |
| Conditions | A closed registry of named condition functions ported from `graph/routing.py`; the policy references them by name with parameters, never code. |
| Agents | Today's roles; one model client function keyed by agent name, behind `mailroom_common.models`; prompts loaded from `config/prompts/<set>/` with id and hash recorded per call. |
| Handoffs | Typed envelopes (sender, confidence, blob refs, request), each an event. Model text is untrusted and never crosses a handoff as free text. |
| Executor | LangGraph with its checkpointer off; the record is the only checkpoint; each model response recorded before the engine acts on it. |
| Replay client | Serves stored step outputs back to the graph; router replay must be identical on every stored document-run (the go or no-go time box). |
| Queue and workers | Content-hash claims (SQLite locally, Postgres in a deploy), leases with heartbeat, breaker per provider, dead-letter after N attempts, spend cap checked before each call from the record's running total. |
| API | Upload and review-resolve keep their shape; review resolution writes a new version (review station). Trace exporters optional, fed from record events. |

**Legacy parity hazards** the engine plan must close first (from the inventory): the 28 skill
files appended to system prompts must be inside the hashed prompt set; `memory.py` feeds an
outcome ledger outside the record into retry prompts and must be recorded or switched off for
`legacy`; most effective prompt text lives in code (`langchain_agents/prompts.py`, 3,198 lines), so
the `deploy_v1` set is materialised to files and compared byte for byte with what the old code
sends (project 4, before the engine).

### 4.4 The launcher (the sandbox, stage 4)

The sandbox becomes `mailroom_launcher`, as the master design says. It keeps the run spec,
preflight, the spend gate with initials and caps, the price sheet check, the Modal and Compose
deploy helpers and the autoresearch driver. It hands its scoring (run metrics, archive gate and
causes, specialist and grid cards, serving parity, schema adherence, routing audit, autoresearch
scoring and keep rule) to the Dojo layers above, and its run-folder reader to the record importer.
It takes over the operator surfaces: the `sandbox` CLI capabilities, the TUI (based on
mailroom-reloaded plus every native sandbox feature) and the floor with recorded replay, all
reading the record. The-Mailroom's server, operator desk and web floor join it, with every
Langfuse or Phoenix trace source replaced by a record tail. Section 8 lists the launcher chunks
from the launcher inventory.

A backend is an adapter with one interface (`start(run_spec) -> run_id`, `fields_reported()`);
a field it cannot report is recorded as absent. Backends: OpenRouter, Modal with vLLM, Docker
Compose, local Ollama or vLLM. Nothing else changes with the backend.

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

| # | Chunk | Waits for | Gate it must show |
| --- | --- | --- | --- |
| 0 | Land PRs #2, #6, #5, #7 (another thread) | - | their own gates |
| 1 | D1 statistics | 0 | old tests and a differential corpus, identical |
| 2 | D2 headline registry metrics in one flat table, and `Measure` | 0 | every old registry id and every metric `scores.json` emits is in the table with the same meaning (or an intended change); every live agent has a `default_view` or a null with a reason; provenance tests |
| 3 | D3a one schema source per class | 2 | YAML equals frozen dump of old tables; labels, Dojo (and later engine) load the same files |
| 4 | D3b tasks, suites, document scorers | 1 to 3 | `scores.json` and the 89 document-scorer goldens reproduced exactly |
| 5 | D4 label layer, Dojo joined to `mailroom_labels` | 3 | old label-policy cases; Protocols checked against the real `LabelSet` |
| 6 | D5 run reader and run layer | importer | old tests; numbers on imported runs |
| 7 | D6 outcome and behaviour (sandbox archive gate, cause codes, routing audit, slots) | 4, 6 | old and sandbox tests; v1 outcome counts |
| 8 | D7 comparison, keep rule, projections, stability, cost truth, log health | 6, 7 | planted-curve property tests; refusal rules |
| 9 | **Stage-2 gate** | 1 to 8 | goldens unchanged; v1 and slice-100 r2 reproduced; Sets A and C scored |
| 10 | Prompt sets materialised (`deploy_v1`, `frozen_v1`, skills, catalog) | 3 | rendered prompts byte-identical to the old code per agent and class |
| 11 | **Go or no-go** (owner) | 9 | - |
| 12 to 17 | E1 foundation, E2 agents and handoffs, E3 policy and compiler and `legacy`, E4 replay, E5 queue and workers, E6 API and exporters | 11 | router replay identical (time-boxed); mock end-to-end equal; at least 249 documents a minute |
| 18 to 21 | Launcher chunks (section 8) | E6 and the record's replay timeline | a mock run per backend with no spend; paid parity runs started by the owner |
| 22 | Cutover: experiment policies B, D, E; Set F | all | Set F pass rule (approved 2026-11-17) |

Chunks 1, 2, 3 and 10 need no record work and start now. Launcher chunks that do not need the
engine (run spec format, preflight, spend gate, price sheet, operator surfaces over imported runs)
may start before the go or no-go gate, because the launcher must also drive the current pipeline
until cutover.

## 7. What is not ported

| Old | Why |
| --- | --- |
| llm-mailroom `audit/`, `storage/`, `result_cache`, `bins`, audit scripts (10,676 lines) | replaced by the record |
| Scoring nodes in the graph, `observability/*` Dojo adapters | the Dojo scores from the record |
| LegalBench, `dojo_source`, `bert_intake`, Langfuse/Braintrust/Phoenix setup and sync, release and pin scripts (13,097 lines) | dropped by the design or obsolete in one workspace |
| The-Mailroom trace sources and `trace_interpreter`, vendored mirrors | replaced by the record and by config |
| Old Dojo `config.py` globals, `scorecard_honesty`, metric tiers, study-specific report modules | replaced by profiles, `Measure` and Dojo reports |

### Capabilities outside the master design's text, and where they land

The master design does not name these. Under the principle above they all move, rebuilt on the
record and configuration; none is dropped.

| Capability today | Lands in | Form |
| --- | --- | --- |
| Gmail intake (`pipeline/gmail_intake.py` 2,722 lines, mail guards, outbox) | engine | an intake channel adapter beside folder and HTTP upload, feeding the same content-hash queue |
| SMS pager, watchdog, status notify, ops monitor | launcher | an operations module that tails the record and the queue; notification transports are adapters, credentials only in env vars |
| Vision classifier (`langchain_agents/classifier.py`) | engine | an agent registered for image documents, model chosen by the run spec |
| Outcome-ledger memory for retries (`memory.py`) | engine | a policy option, off in `legacy` unless replay shows it on; when on, its inputs are record events so replay stays exact |
| Regex post-hoc ground truth (`posthoc_gt.py`, `extraction_gt.py`) | Dojo | a diagnostic report, never a label layer (R6 precedence is unchanged) |
| Hosted Observatory (`hosted/`, `server/hosted.py`) | launcher | a read-only floor view over a sealed record export with no document text |
| React desk UI (`ui/`) | review station (other thread) | joins the review station's UI plan |
| The-Mailroom visualizer | launcher floor | reads the record; the old repository stays on its pin (master design non-goal), so today's trace span names are not needed at cutover |

## 8. Launcher chunks

To follow, from the sandbox and mailroom-reloaded inventory (in progress).

## 9. Trade-offs

| Choice | Gives | Costs | Revisit when |
| --- | --- | --- | --- |
| Schema files as the contract, three small loaders | No shared package, each package installable alone | Three loaders to keep in step (a workspace test checks them) | A fourth reader appears |
| `Measure` instead of an honesty module | One rule, impossible to print a bare number | Every layer returns a richer type | Reports become slow to build |
| Golden parity before new metrics | Old numbers stay comparable across the cutover | New layers wait for D3b | The old Dojo is retired |
| Launcher work before the go or no-go gate | Operator surfaces over imported runs early | Some launcher code drives the old pipeline first | The engine is built |
| Port by verdict, not by file | About a third of the code never moves | Each drop needs a reason in the inventory | A dropped capability is asked for |

## 10. Owner decisions

Decided 2026-11-18:

1. Keep the `common` and `dictionaries` leaf packages (section 3).
2. Adopt the ruff, mypy and codespell gates (section 3).
3. The launcher floor replaces The-Mailroom at cutover; the old visualizer stays on its pin, so
   the trace exporter needs no span-name compatibility (answers master design open question 2).
4. The headline registry metrics stay, improved on the new system, with consistent meanings
   (section 4.2).
5. Per-agent defaults (2026-11-18): the boss is judged on wrong approvals; intake on the
   accuracy of its triage class alone, the clerk diagnostics dropped; the transcribers are
   deferred; gold is the interim and near_gold labels, the Hub ground truth and additional
   generated gold, never waiting for human resolutions, and the decision agents' gold is
   derived from those layers (D2 plan).

Still open: confirm the placements of the capabilities in section 7 that the master design does
not name; confirm the D2 default views marked as proposals and the proposed metric directions.

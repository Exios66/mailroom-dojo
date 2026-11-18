<!-- Exported from the Claude Doc "Lean Mailroom Rewrite Design" (approved by JJB 2026-11-17 CT). The Doc is the live copy; this file is the snapshot the build plans argue from. The "Requirements added 2026-11-18" section is recorded here first and is part of the master plan. -->

# Lean Mailroom Rewrite Design

2026-11-18 · JJB

## Requirements added 2026-11-18

JJB added these during the build, after the design was approved. They are part of the master plan: every project plan names the requirements it serves and how its gate checks them, and where one conflicts with older text in this document, the requirement wins.

| # | Requirement | Projects that must show it |
| --- | --- | --- |
| R1 | Fit American Family Insurance's problem better than the current pipeline, and be a document-intelligence mailroom ingress benchmark | All; measured by the Dojo and Set F |
| R2 | Entity extraction quality is CRITICAL | Dojo (per-field reports), prompts, engine |
| R3 | Cheap, correct, efficient classification that keeps the sorter-to-specialist hand-off, tuned to the mailroom dataset | Dojo, prompts, engine |
| R4 | Maintainable, modular, customizable: a new document type is config and plug-ins, no code fork (AmFam has many more types than the dataset) | Record (domain-free), labels, Dojo, engine, launcher |
| R5 | Provider-agnostic, with every current launch path: HTTP API, Docker Compose, Modal with vLLM (plus OpenRouter and local) | Engine, launcher |
| R6 | All scoring built from the mailroom dataset and the interim and near_gold Claude labels, saved in this repo as versioned snapshots, for the highest data quality | Labels, Dojo |
| R7 | The CLI, the TUI and all floor replay move into this repo; the TUI starts from `mailroom-reloaded` and adds every native sandbox CLI and TUI feature | Record (replay timeline), launcher |
| R8 | Tests exercise real behavior and detect every error state and point of failure, never only one-off unit tests | All; see `docs/test-strategy-record.md` |
| R9 | Stays tunable and maintainable as goals shift, with no repeat of the old multi-repo dependency mess: minimal coupling, clear package boundaries, config-driven tuning, and a human-logical file layout | All; layout map below and in `README.md` |
| R10 | Decision-model experiments are easy to set up, as in the current pipeline: the slot design from mailroom #309 carried over as policies and config, every decision recorded, scored by the Dojo | Record (decision events), Dojo, engine, launcher |

### R1. Fit AmFam's problem and become a benchmark

**Fit American Family Insurance's problem better than the current pipeline does, and be modular, expandable and customizable enough to be a true document-intelligence mailroom ingress benchmark.**

#### Success criteria for AmFam

Each is measured by the Dojo on the same draws, new stack against the current pipeline, and reported per document class.

| Need | Criterion |
| --- | --- |
| Fewer wrong archives | Wrong-archive rate at or below the current pipeline's at the same review rate, and at most 2% per class where the class can reach it (the Set F rule) |
| Less human review | A lower review rate at the same wrong-archive rate, and no document parked because of a provider outage |
| Lower cost | Cost per 1,000 documents within 10% of today's at worst, reported beside the review cost it saves |
| Auditability | Every decision rebuilt from the record alone; every chain verifies; document versions (original, transcript, attempts, archived record, human corrections) visible to a reviewer |
| Throughput | At least 249 documents a minute with the fake model and durable writes on |

### R2 and R3. Extraction quality and sorter-to-specialist classification

JJB, 2026-11-18. Both are CRITICAL and carry into the Dojo, prompts and engine plans.

1. **Entity extraction quality.** Field-level extraction is scored by field type against the committed labels, per class and per field, with coverage and precision reported separately. Each specialist's extraction is a first-class Dojo report and a first-class optimization target for prompts (project 4). A change that lowers per-field accuracy on any class is a regression even if utility rises.
2. **Cheap, correct and efficient classification with specialist hand-off.** Ingress keeps the current pattern: a sorter classifies the incoming document, then hands off to a specialist tailored to that document type, tuned to the mailroom dataset. The engine keeps this as its default graph shape (other shapes are policies, not replacements). The Dojo reports class accuracy, silent-wrong per 1,000, cost per classification and the hand-off's effect on extraction.

| Focus | Success metric (per class, new stack against the current pipeline, same draw) |
| --- | --- |
| Extraction quality | Per-field accuracy and coverage at or above today's on every class; mean field score up on the classes below the bar |
| Classification | Class accuracy at or above today's (100 of 100 on v1), silent-wrong per 1,000 not higher, cost per classified document not higher |

### R4 and R5. Modular, extensible document types, provider-agnostic launch paths

New clients and document types plug in through configuration and registered plug-ins, never through a code fork.

| Extension point | How it plugs in | Where |
| --- | --- | --- |
| Document classes and subclasses | A class registry entry (name, subclasses, thresholds) in a client profile | Labels and Dojo read it; the engine routes on it |
| Extraction schemas | A schema per class, versioned, with field types the Dojo scores by type | Labels and Dojo |
| Nodes and agents | Registered node types with typed handoffs; a policy wires them | Engine |
| Routing policies | A policy file: graph, thresholds, decision slots; its hash is stored on every run | Engine |
| Prompts | Prompt sets as overlays on a default, each hashed into the run | Engine, measured by the Dojo |
| Backends | A backend adapter that reports the fields it can; the rest are recorded as absent | Launcher |
| Labels | Label snapshots per client with status and source | Labels package |
| Scoring and reports | Registered metrics and report builders over records | Dojo |

**Adding a document type is cheap.** AmFam has many more document types than the mailroom dataset covers (JJB, 2026-11-18). A new type is added by a client profile entry and plug-ins only: a sorter label (with its description and thresholds), an extraction schema, a specialist prompt (or a reuse of a generic specialist with the schema), and a scorer configuration. No code fork and no edit to the engine, Dojo core or record. Each project plan includes a test that adds a synthetic document type this way end to end.

**Provider-agnostic, every launch path.** No agent, prompt or policy names a provider or a model; the agent-to-model map lives in the run spec and client profile. The launch paths the current versions support stay supported: the HTTP API, the Docker Compose producer, and Modal with vLLM, plus OpenRouter and local Ollama or vLLM. A launch path is a backend adapter; it never changes the graph.

The record package is domain-free: it stores node names, class labels, decision payloads and blob roles as data, so no class, node or client is hard-coded in it. A benchmark run is a client profile plus a policy plus a label snapshot plus a draw; anyone can add a new one without changing the record, the Dojo's core or the engine's executor.

### R6. Scoring from the mailroom dataset and Claude labels, saved in this repo

- Every score in the new Dojo is computed against the pinned mailroom dataset (`Lucius-Morningstar/mailroom-dataset`, by revision) and the label layers in force today: Hub, then interim, then near_gold (Claude-generated), with gold reviewer decisions on top. Precedence does not change.
- The interim and near_gold labels, and the gold-label review decisions, are committed to this repo as versioned label snapshots (project 2). Each snapshot has a manifest hash; a run records the snapshot it was scored against.
- Data quality is a gate, not a hope: a snapshot is checked against the dataset's content hashes and against the closed label vocabularies before it can be used, and documents whose content hash does not match are flagged as unverified, never silently scored. Experiment runs, tests and calibration all read the same committed snapshots.

### R7. CLI, TUI and floor replay move into this repo, starting from mailroom-reloaded

- The sandbox's operator CLI capabilities, its TUI and the floor viewer with recorded replay are rolled in, reading the run record instead of sandbox run folders (launcher project, with the TUI as part of it).
- The TUI starts from the TUI enhancements in `LLM-Mailroom-Services/mailroom-reloaded` (JJB, 2026-11-18) and is extended with every native sandbox CLI and TUI feature; the launcher project's plan inventories both before building.
- The record must carry what the floor needs: node start and finish times, node status (running, done, paused, failed), step, stage, agent and model, document outcomes, and the run's terminal state, cursor and total. Project 1 stores these and provides a replay timeline built from the record that matches the sandbox's recorded-replay contract (`tui/floor_replay.py:build_timeline`), so any stored or imported run can be replayed on the floor.

### R8. Tests that detect failure

Every module is tested against real behavior: real killed processes, real concurrent threads and processes, injected disk-full, short-write and fsync errors, corrupt and truncated files, forked chains and tampering, each asserting the exact error or problem reported, never a silent pass. Property-based tests check the record's invariants over random operation sequences, and each stage gate runs end to end on the stored runs. The full bar and the fault matrix are in `docs/test-strategy-record.md`; later packages get the same treatment in their own strategy.

### R9. Tunable, maintainable, and laid out so anyone can find things

The old constellation became a dependency mess as goals moved: pins between repos, vendored copies, scoring split three ways. This workspace avoids that by construction. Dependencies point one way (record, then labels, then the Dojo and the engine, with the launcher alone composing them), enforced by a test. Tuning (classes, schemas, thresholds, policies, prompts, models, labels) is configuration and data, versioned in this repo and hashed into each run, so a goal change is a config change and a measured run, not a code fork. Every task keeps to this map:

```
mailroom-dojo/
├── README.md                  what this is, the layout, the test command
├── packages/                  one folder per package; each is installable alone
│   ├── record/                mailroom_record: run record, blob store, index, verify, replay (project 1)
│   ├── labels/                mailroom_labels: label snapshots and coverage (project 2)
│   ├── dojo/                  mailroom_dojo: scoring, comparison, reports (project 3)
│   ├── engine/                mailroom_engine: agents, handoffs, policies, API (project 5)
│   └── launcher/              mailroom_launcher: run specs, backends, CLI, TUI, floor (project 6)
│       (each package: pyproject.toml, README.md, src/<module>/, tests/, scripts/)
├── config/                    everything tuned without code (from project 2 on)
│   ├── clients/<client>/      a client profile: document classes, schemas, thresholds, label snapshot
│   ├── policies/              routing policies (one file each; its hash is stored on every run)
│   └── prompts/<prompt_set>/  prompt sets; variants are overlays on the default set
├── labels/<snapshot>/         committed, versioned label snapshots with manifests (project 2)
├── docs/
│   ├── specs/                 the design (the master plan)
│   ├── plans/                 one build plan per project, dated
│   ├── adr/                   architecture decision records
│   ├── test-strategy-*.md     one test strategy per package
│   └── reports/               gate reports, written by scripts
└── .circleci/                 CI (CircleCI only)
```

Rules: dependencies point one way and a test enforces them: `labels` imports `record`; `dojo` imports `record` and `labels`; `engine` imports `record` (and reads client profiles from `config/`); `launcher` may import all of them; the Dojo never imports the engine, and `record` imports nothing of ours. Tuning lives in `config/` and `labels/`, never in code. A new folder needs a line in this map. Names say what a thing is, in full words; nesting goes from general to specific.

### R10. Decision-model experiments by configuration

Decision models stay a first-class experiment, as they are on the current pipeline. The eight decision slots of mailroom #309 carry over: sorter reviewer, archive gate, judge replacement, judge check, boss check, arbiter veto, closed-field resolver and field review (field-level routing), plus the default archive checks that need no model. Each slot is off by default, has its own caps (including per-slot override caps for slots that can reroute), and is switched on and tuned in a policy file or run spec. An experiment arm is a named policy plus config, with no code change per experiment.

Every slot call is an event in the record (`kind="decision"`, the slot as `node`, the model, tokens, latency, and the decision with its probability and cause as our own tokens), so a run can be replayed and audited per slot. The Dojo scores slots from the record: per-slot accuracy and override rate, silent-wrong per 1,000, cost per decision, and the risk-coverage curve with bounds (Sets A, B, D and E).

## Summary

Build one new workspace that replaces llm-mailroom, llm-dojo-scoring and most of the sandbox with three small packages around a single run record. The current repos keep shipping the 0.30 line while this is built, and the new stack takes over only after it reproduces today's results and Set F confirms it.

- **Record:** one append-only, hash-chained log per run, plus document versions stored once by content hash. It is the audit trail and the analysis data at the same time.
- **Engine:** the multi-agent pipeline as a graph of agents and handoffs, run by a named routing policy. A `legacy` policy reproduces today's behavior.
- **Dojo:** the only place that scores a run, whatever backend produced it. It adds behavior scoring, run statistics and fitted projections of cost and experiment size.
- **Launcher:** what remains of the sandbox: run specs, preflight, the spend gate and deploys.

_[Diagram: how the parts fit · 4 packages around one record — see the Claude Doc]_

The launcher and engine only write the record and the Dojo only reads it, so a score can always be rebuilt from stored runs.

Why now: the v1 failure analysis shows utility is lost in routing, not classification (100 of 100 correct), and the scoring that proves it is split across three repos. The Dojo is about 31,000 lines with 284 exported names, of which its own consumer audit finds 38 in use, while about 10,000 lines of experiment scoring sit in the sandbox.

### Decisions JJB made

| Decision | Choice |
| --- | --- |
| Scope | Full rewrite of both the pipeline and the Dojo, now |
| Routing | A config. The engine runs a named policy, `legacy` must match today's behavior, and new policies are what Sets A to F compare |
| Current tree | Keeps shipping. The 0.30.0 Compose deploy and PRs #309 and #310 go ahead. Sets A, the offline half of B, and C may start on the current stack |
| Dojo scope | Absorb all sandbox-only scoring, including the Modal specialist-only runs. Benchmark full-pipeline runs from OpenRouter, Modal with vLLM, Docker Compose and local variants. Add missing scoring |
| Storage | Fix slow databases, bloat, lost records, hard tracing, hard analysis, and missing document versions for human review |
| Shape | One new workspace, built record first |
| Engine style | A graph that can be viewed as a graph: multi-agent, with handoffs, able to take high volumes of incoming documents |
| Labels in the workspace (2026-11-18, during the build) | All scoring is still built from the mailroom dataset and the interim and near_gold Claude labels. Those labels are saved into this repo, versioned, for the highest data quality in experiment runs, testing and calibration |
| Operator surfaces (2026-11-18, during the build) | The CLI capabilities, the TUI and all floor replayability (the sandbox floor viewer and its replay) are rolled into this repo seamlessly |

JJB added two items after the first draft: mergers must improve significantly using the new merger schema built on MAUD-style questions, and the EDA repository with all interim label work is rolled in. Both are in the Labels and mergers section. The merger item replaces the earlier "lowest priority" standing.

## Goals, non-goals and anti-goals

### Goals

1. **One scorer.** Any run is scored by the Dojo and nowhere else: specialist-only or full pipeline, on any backend, old or new.
2. **Routing is a policy.** Every experiment arm is a named, hashed policy file. `legacy` reproduces today's routing exactly.
3. **One record.** Every step of every run lands in one hash-chained log. Any document can be traced to its runs, steps and versions with one command, and back.
4. **Storage that stays small and fast.** A document is stored once however many runs touch it. Analysis never locks the live database.
5. **Statistics fit for full-pipeline runs.** Paired comparisons, noise floors from replicates, risk-coverage with confidence bounds, calibration, and cost per agent, class and outcome.
6. **Fitted projections.** Cost and experiment size for larger volumes of similar documents, with intervals and a stated range of validity.
7. **A graph you can look at.** The pipeline is a multi-agent graph with explicit handoffs, drawn from the same file that runs it, and built for high incoming volume.
8. **Nothing already paid for is lost.** The Dojo reads the run folders the current sandbox writes.

### Non-goals

- Choosing models and thresholds. Those are what the experiments decide. This design makes them measurable. Merger extraction and prompt optimization are in scope, under Labels and mergers and Prompt optimization.
- Changing the 0.30 line, its deploy, or PRs #309 and #310.
- Moving the other Dojo users (The-Mailroom visualizer, the EDA repo, eval-environment, mailroom-ml, the hub). They stay on the commit they pin.
- Working the open Dojo issues, which another agent owns.
- New document classes or a new review interface.

### Anti-goals

These would make the rewrite a failure even if it runs.

- A score that differs from the current Dojo on the same input with no recorded reason.
- A `legacy` policy that matches "approximately".
- A second bloat. An exported name or a stored field with no reader does not ship.
- A projection quoted without an interval, or far outside the measured range without a warning.
- Two stores that must be reconciled with each other.
- A pipeline that needs a trace service to know what it did. Trace sinks are optional exports.
- A test that calls a real model, or a paid run that starts without a person's initials.

## Constraints

### Carried through unchanged

This list is Claude's proposal. JJB was shown it and did not object.

- The taxonomy file as the single source of classes, thresholds and the agent-to-model map.
- The extraction schemas and closed-label vocabularies.
- The `deploy_v1` prompt set as the default. Every model call records the prompt id that actually ran.
- The producer API: upload and review-resolve.
- The audit properties: hash-chained, verifiable after the fact, one row of truth per document.
- Label precedence: Hub, then interim, then near_gold. A corpus hash mismatch flags a document as unverified and does not block scoring.
- The spend gate: preflight, then a start signed with a person's own initials, with wall-time and cost caps on every paid run.
- Model access through one client function keyed by agent name. Documents are never truncated; long inputs use sliding windows.

### Rules that still bind

- No session links or tool footers in commits, PRs or tags. CI is CircleCI only.
- No real model call in any test. Paid and hosted-write actions are started by a person.
- Python 3.11 or later. The Dojo's core dependencies stay small; numerical fitting sits behind an optional extra.
- No document text in any exported analysis file. The repos are public.

### Facts the design must respect

- Stored runs are the only baselines we have: v1 (25 documents, 4 runs) and slice-100 r2 (95 documents). The new Dojo must reproduce their numbers before it is trusted with new ones.
- Classification was 100 of 100 on v1. Per-class token profiles come from 5 documents per class, so early cost fits carry wide intervals.
- Throughput on the current pipeline is 249 to 276 documents per minute in the mock benchmark after PR #297. The new engine must not fall below that.

## Approaches

JJB chose the first of three. All three share the run record at the centre and differ in where the code lives and what is rewritten.

| Approach | What it is | Gains | Costs |
| --- | --- | --- | --- |
| **1. One new workspace, record first (chosen)** | A single new repo with three packages, built in the order record, Dojo, engine, launcher | One version and one CI. No commit pins or vendored copies between the parts. The Dojo is useful before the engine exists | Breaks the three-repo ownership model. The hub and other Dojo users stay on the 0.30 line until they move |
| 2. Same design in the three repos | A v1 branch in each repo, with the record contract owned by the Dojo | Ownership and downstream repos unchanged | Every change to the record becomes a pin bump and a vendor refresh while the record is still being designed. Slowest |
| 3. New Dojo and store, pipeline slimmed in place | Record, store and Dojo are new. The current pipeline is cut down, not rewritten | Lowest risk that `legacy` fails to match. Fastest to the experiments | Keeps most of the pipeline's 17,000 lines and 22,000 lines of scripts. Less than the rewrite asked for |

Why the first: the friction today is the chain between repos. An experiment on the decision slots currently waits on two merges and a vendor refresh on JJB's machine. A single workspace removes that, and building the record first means stored runs can be re-scored before any pipeline code is written. Approach 3 stays the fallback if time gets tight.

One point changed after the choice. Claude proposed a plain step runner for the engine so that replays are exact. JJB asked instead for a framework that can be viewed as a graph, with multi-agent handoffs and high-volume intake. The engine section follows JJB's direction and gets exact replay a different way.

## The run record and store

The record is the contract between everything else: the engine and launcher write it, the Dojo reads only it. Today the same facts are spread over the pipeline's audit table, the sandbox ledger and run folders, the experiment log and the trace sink.

### What a record holds

| Part | Contents |
| --- | --- |
| Run | Id, kind (specialist-only or full pipeline), backend label, policy name and hash, prompt set, agent-to-model map, code version, label snapshot ids, the document draw, caps, who signed it |
| Event | One entry per step, append-only: run, document, sequence number, agent, attempt, kind (handoff, model call, decision, output, review, error), hashes of input and output, model, prompt id, tokens in, out and cached, price basis, latency, decision and cause token, previous hash, own hash |
| Blob | Anything with content, stored once under its content hash and compressed: the original file, the transcribed text, each extraction attempt, each verdict, the archived record, each human correction |
| Version chain | Per document, the ordered list of blobs with their role, so a reviewer sees original, transcript, every attempt, what was archived and what a person changed |

A production deploy uses the same record. There, a run is a service period, not an experiment.

### How it is stored

- **Events** are appended to one segment file per run while it is live, then sealed into column files when the run ends.
- **Blobs** live in a content-addressed folder. A document seen in 20 runs is stored once.
- **A small live database** (SQLite locally, Postgres in a deploy) holds only indexes: runs, documents, chain heads and the review queue.
- **Analysis** reads the sealed column files through an embedded query engine, so it never touches the live database.
- **Trace sinks** (Langfuse, OpenTelemetry) are optional exporters fed from events. Nothing reads them back.

### Integrity

Each document has its own hash chain within a run, so documents processed in parallel never wait on a shared chain lock, as they do today. When a run is sealed, the heads of its document chains are combined into one run hash. A verify command recomputes both levels and names the first broken entry.

### How each storage pain is answered

| Pain JJB named | Answer |
| --- | --- |
| Slow databases | Live database holds indexes only. No global chain lock. Analysis reads sealed files |
| Storage bloat | Content-addressed blobs with compression. Sealed runs pack into one archive file. A prune command with a stated retention rule |
| Lost records | Append-only log. A run is resumable from its own record. Verify reports gaps, not just bad hashes |
| Hard to trace runs | `trace run` lists documents, steps and outcomes. `trace doc` lists every run and version of one document |
| Hard to analyze | One schema for every run kind and backend, in column files |
| Document versions for review | The version chain, including human corrections as new versions |

### Reading what exists today

An importer turns a current sandbox run folder (`items`, `nodes`, `dataset` and `events` files plus the locked spec) into a record marked as imported. A field the old run did not capture is recorded as absent, never guessed. Importing the 0.30 deploy's audit database is wanted later but is not part of the first build.

## The Dojo

The Dojo is a set of pure functions from a record plus labels to scores, statistics, projections and reports. It never runs a model and never reads anything but records and label files.

### What it scores

| Layer | What it answers | Where it comes from |
| --- | --- | --- |
| Labels | Which ground truth applies to each field, its status, and whether a document is scorable | Dojo label policy plus the sandbox scorable-field count |
| Fields | How right each extracted value is, by field type, including CUAD categories, MAUD questions and closed vocabularies | Current Dojo field scoring, kept score-for-score |
| Document outcome | Correct, review, unscorable, wrong or crash at the quality bar, with a cause and a utility | Sandbox archive gate and cause code |
| Behavior | For each oversight step: was it right when it ran, and what happened when it was skipped. Handoff paths, retries, per-slot summaries and override caps | **New**, plus the sandbox routing audit and Dojo PR #182's slot summary |
| Run | Classification metrics, wrong-archive and review rates with bounds, the risk-coverage curve, schema adherence, cost per agent, class and outcome, and serving figures (speed, latency, GPU time) by backend | Sandbox run metrics, specialist cards, serving parity, Dojo PR #182 |
| Confidence | Does a reported confidence predict quality: calibration error and ranking power per agent and class | Dojo calibration code, applied to routing confidence. **New** as a standing report |
| Stability | How many documents change outcome between identical runs, and the noise floor that implies | **New.** Done by hand in the v1 analysis |
| Cost truth | Token-priced estimate against the billed bound and against key-balance change | **New.** The v1 gap was 1.8 to 1.9 times |
| Log health | Share of steps with complete entries, and whether every chain verifies | **New** |

Specialist-only runs use the label, field, run and confidence layers. Full-pipeline runs use all of them. The backend is a column, never a branch in the code.

### Comparing runs

- Paired by document wherever two runs share a draw, with bootstrap intervals for score differences and a paired test for outcome flips.
- Every comparison states its noise floor, taken from replicate runs where they exist.
- The keep rule from the autoresearch loop moves here, with its per-class guard rewritten to respect per-class noise. The v1 guard fired on noise in both test rounds.
- Every number carries its sample size, its cost basis and the status of the labels behind it. This one rule replaces the current scorecard-honesty module.

### Projections

The Dojo fits curves only where a relationship is curved, and says which form won.

| Question | What is fitted | Notes |
| --- | --- | --- |
| What will N documents cost? | Tokens per document against document length, per class and agent, then priced through each model's tiers | Total cost is linear in N. The curve is in length and in price tiers: Qwen's rate triples above 32,000 tokens. Routing adds a measured multiplier per policy for judge passes and retries |
| How sure is that? | Resampling measured documents to the target class mix and length profile | Gives dollars per 1,000 documents with an interval that narrows as N grows |
| How many documents does an experiment need? | Noise half-width against sample size, as a power law | The expected exponent is one half. A fitted value far from it signals clustered documents |
| How many to certify the 2% bar? | Exact binomial bounds | About 150 scorable archives with no errors, 237 with one, 315 with two |
| What does more spend buy? | Quality against cost across model and oversight tiers, as logarithmic and saturating forms | Shows where returns flatten |
| How does a backend scale? | Throughput against concurrency, as a saturating form | For Modal and local serving |

Every fit tries linear, power, exponential and logarithmic forms, picks by an information criterion and by leave-one-out error, and reports parameters, an interval, the measured range and a residual check. It refuses to fit fewer than five distinct points. It warns when asked to project beyond three times the measured range. The user supplies a volume, a class mix and a length profile, or names a measured slice to copy them from.

### What is dropped

A module survives only if a gate, a report or a projection reads its output. On that rule the following go: the metric registry's tiers, bundles and profiles (one flat metric table replaces them), the legacy flat import paths, the release-awareness tool, LegalBench scoring, and the prompt catalog, which moves to the engine. The mailroom-ml label contract stays on the 0.30 line with its user.

### Proof it is right

1. The current Dojo's golden score cases pass through the new field scorer unchanged. Any intended difference is written to a score changelog with its reason.
2. Re-scoring the imported v1 runs gives the known outcome counts: baseline 6 correct, 5 review, 7 unscorable, 7 wrong; replicate 5, 5, 8, 7; mean utility -0.101 and -0.140.
3. Re-scoring slice-100 r2 gives 40 of 55 wrong archives and 9 of 95 reviews.

## Labels and mergers

Ground truth becomes a fifth package in the workspace, because every score is only as good as the labels under it and that work is spread over three places today.

### Labels package

| Today | Where | In the new workspace |
| --- | --- | --- |
| Refined ground-truth master and its labeling tools | The EDA repository (`Mailroom-Corpus-EDA`), outside this project's repos | Moved in as the labels package, with its history of who labeled what |
| Interim overlay and near_gold catalog | Sandbox `data/labels` and the gold-label scripts | Moved in as label snapshots |
| Gold-label review decisions (correspondence, corporate owner, missing fields) | Project files `gold-label-review/` | Moved in as reviewed overlays on the snapshots |
| Label policy, scorable-field rules, stale-label handling | The Dojo | Stay in the Dojo, reading the labels package |

- **A label snapshot** is a file set with a manifest hash. A run records the snapshot it was scored against, so a score can always be tied to the exact labels.
- **Every label keeps its status** (Hub, interim, near_gold, gold with reviewer initials) and its source. Precedence is unchanged.
- **Coverage is a first-class report:** scorable documents and fields per class, by label status. The v1 analysis found 8 of 25 documents unscorable because only `intent` was labeled, and that hides any change in those specialists.
- **In deployment**, human corrections from the review queue are written as label candidates with the reviewer's initials. That is how production use widens label coverage.
- Tests and the proof gates read committed snapshots. Nothing fetches labels over the network at test time.

### Mergers

Mergers were the worst class in v1: no merger archive reached the bar, mean score 0.12 to 0.29, and 74% of run cost. The measure was also weak. The gold collapses several MAUD sub-questions under shared names, so only a 12-question clean subset has an unambiguous target.

- **Schema.** The merger specialist extracts against the current merger schema (21 scored fields plus the MAUD clause answers). Each MAUD-style question is a closed choice with a fixed option list, and each maps to exactly one ground-truth label. A question with no unambiguous label is not scored, and is listed as unscored.
- **Scoring.** Per question: answered or not, right or wrong, with coverage and precision reported separately. v1 showed a model can double its answers with no gain in precision, and a single blended score hid that.
- **Extraction.** Closed-choice questions suit the decision-model slots and a per-question pass over the relevant section, not one pass over a 500,000-token document. Which method wins is an experiment (Set E, widened), not a design assumption.
- **Routing.** Until merger extraction reaches the bar, the policy parks mergers for review with the answers attached as a prefill.

The exact question list and its mapping to labels must be confirmed against the Dojo's MAUD schema and the EDA labels during stage 2. Claude has not read the EDA repository yet.

## Prompt optimization

JJB added this after the first draft: every specialist and agent prompt is to be optimized. The design treats a prompt as a versioned input that the Dojo can measure, the same way it treats a routing policy.

- **Default.** The repo's `deploy_v1` set stays the production default. A trace service never supplies prompts. Every model call records the id and hash of the prompt that ran.
- **Variants.** A variant is an overlay on a prompt set that changes one agent's prompt. A run names its prompt set and overlays, so two arms differ in exactly one prompt.
- **What each agent is measured on.** A prompt is judged by the Dojo layer its agent controls.

| Agent | Measured by |
| --- | --- |
| Sorter, sorter reviewer | Class accuracy, silent-wrong per 1,000, park rate |
| Specialists | Paired per-field scores, with the per-field miss table as the list of targets |
| Judge, arbiter, boss | Behavior layer: was the verdict right when it ran, wrong archives let through, reviews that were not needed |
| Intake, relations, triage | Their own output fields, plus effect on downstream outcome |

- **Targets come from measured misses.** For example, the v1 contract runs missed the same CUAD categories in 4 of 4 runs. Those categories are the first targets, not general rewording.
- **Loop.** The launcher's autoresearch loop proposes a variant, runs it on the development pool, and the Dojo's keep rule accepts it only if the paired gain clears the noise floor with no class getting worse. Slice-500 is never used for tuning.
- **Checks before any paid run.** Each prompt names every field of its schema and every allowed value of its closed vocabularies. This check exists in the Dojo today and moves to the engine's tests.
- **After a prompt changes,** confidence thresholds are refit, because prompts move reported confidence.
- **Order.** Specialists first, since extraction is where utility is lost, then the oversight agents. Sorter prompts last: classification was 100 of 100 on v1.

### Optimized for the agent's place in the graph

JJB's direction: each prompt is tuned for its node's role in the framework, for routing, utility and performance in that role, and must not depend on any one provider.

- **A prompt is written against its handoff contract.** It states what the agent receives from the node before it and exactly what the next node needs from it: the fields, the evidence references, and a confidence the router can use.
- **Confidence is part of the job.** v1 found the specialists' reported confidence took five values and did not track quality, and that alone decided most wrong archives. A specialist prompt variant is scored on how well its confidence predicts its own field scores, not only on the field scores.
- **Utility is the objective.** A variant is kept on whole-pipeline utility and the wrong-archive rate at a fixed review rate, with the agent's own layer as the diagnostic. A prompt that scores better alone but routes worse is rejected.
- **Provider-neutral.** A prompt may not rely on one model's quirks. A variant must hold its gain on at least two worker models from different providers before it becomes the default. The plain-text request for JSON output stays, since some models reject requests without it.
- **Where it lands.** Accepted prompts and the improved merger schema go to the current shipping tree as a new prompt set and schema version, and into the new workspace unchanged.

This builds on the existing prompt alignment plan and the `deploy_v1` set. Claude has not re-read that plan for this draft, so the builder should reconcile the two.

## The engine

The engine is a multi-agent graph whose shape lives in the policy file. The same file is drawn as a picture and compiled into the running graph, so what you look at is what runs.

### Agents and handoffs

- **Agents** keep today's roles. Model-backed: intake, sorter, sorter reviewer, the five specialists, judge, arbiter, boss, relations, triage. Procedural: transcriber, image extractor, reporter, archivist. Human review is a node like any other.
- **A handoff** is a typed envelope passed along an edge: what the sender did, its confidence, references to the blobs it produced, and what it asks of the receiver. Every handoff is an event in the record. That is what makes behavior scorable.
- **An agent** takes the current state and returns a partial update. It reaches a model only through the one client function and records the prompt id it used.

### Policies

A policy is a named, versioned file whose hash is stored on every run. It holds the nodes, the edges and their conditions, the thresholds, the decision-slot settings with their caps, and the retry budgets. It cannot change schemas or prompts.

| Policy | Purpose |
| --- | --- |
| `legacy` | Today's routing, including the eight decision slots at their baseline setting and the default archive checks from PR #309 |
| Experiment policies | One per arm of Sets B, D and E, for example always judging contracts, parking every merger, or a decision-model archive gate with a 10% cap |

Oversight steps share one shape: a check returns a verdict and its support, and the policy says what a verdict may do. A check may always add a review. It may release a document from review only where the policy allows it, under a cap.

### Running the graph

The policy file is compiled into a LangGraph graph, the framework the current pipeline uses. The engine owns the graph definition, so the executor can be swapped without touching policies. `engine graph --policy <name>` draws the graph from the same file.

Exact replay does not depend on the executor. It comes from the record: a replay client serves stored step outputs back to the graph, so the router can be re-run on any stored run with no model call.

### High volume

- Documents are claimed from a queue by content hash, so a duplicate upload is processed once. SQLite claims locally, Postgres row claims in a deploy.
- Async workers with a concurrency limit per provider, so a slow provider applies back-pressure and does not pile up requests.
- One model client per process. Windows of a long document run in parallel.
- The record is the checkpoint. A stopped run resumes from its last complete event.
- Floor: at least today's 249 to 276 documents per minute in the mock benchmark.

### Proof that `legacy` matches

1. **Router replay.** For every stored document-run (100 in v1, 95 in slice-100 r2), feed the stored step outputs to the `legacy` policy. The path of handoffs and the final outcome must be identical.
2. **Mock end-to-end.** The fixture set gives the same outcomes on the old pipeline and the new engine with the fake model.
3. Every mismatch is listed. Each is fixed, or signed off by JJB as an intended difference and written down. There is no tolerance band.

### The outside surface

The upload and review-resolve API keep their shape. Human review reads the version chain and writes each correction as a new version.

## The launcher and backends

The launcher is what the sandbox becomes: it starts runs and writes records, and it scores nothing.

- **It keeps:** the run spec, preflight, the spend gate with initials and caps, the price sheet check, the Modal and Compose deploy helpers, and the autoresearch loop as a driver that proposes the next policy.
- **It hands to the Dojo:** run metrics, the archive gate and causes, specialist cards and grid cards, serving parity, schema adherence, the routing audit, and the autoresearch scoring and keep rule.
- **It retires:** the study-specific report modules. Their tables become Dojo reports rebuilt from records.
- **It takes over the operator surfaces:** the `sandbox` CLI capabilities, the TUI and the floor viewer with recorded replay, all reading the record (the replay timeline comes from the record package). The TUI is based on the `mailroom-reloaded` TUI enhancements plus all native sandbox CLI and TUI features.

| Backend | How it runs | What it adds to the record |
| --- | --- | --- |
| OpenRouter | Engine calls the hosted API, including the Decisions endpoint for decision slots | Provider, billed-bound cost, cached tokens |
| Modal with vLLM | Engine or specialist-only job on the Modal worker, models served by vLLM | GPU seconds, billed window, time to first token, tokens per second |
| Docker Compose | The producer image with API and watcher | Service-period runs, queue depth, throughput |
| Local variant | Ollama or a local vLLM | Same serving fields where the server reports them |

A run spec names its backend, its kind and its policy. Nothing else in the stack changes with the backend. A field a backend cannot report is recorded as absent.

## Build order and proof gates

Each stage ends in a gate that must pass before the next stage is trusted. Each stage gets its own implementation plan.

| Stage | Builds | Gate |
| --- | --- | --- |
| 1. Record and store | Record schema, event log, blob store, live index, verify, trace, pack and prune, the sandbox-run importer | The v1 runs and slice-100 r2 import cleanly, every chain verifies, both trace commands work, and stored size is reported against today's |
| 2. Dojo | The scoring layers, run comparison, projections, reports | Golden cases unchanged. v1 and r2 numbers reproduced. Then it scores Sets A and C from runs made on the current stack |
| 3. Engine | Agents, handoffs, policy compiler, replay client, the `legacy` policy, queue and workers, the API | Router replay matches every stored document-run. Mock end-to-end matches. Throughput at or above the floor |
| 4. Launcher and backends | Run spec, preflight, spend gate, the four backends | A mock run per backend that needs no spend. One paid parity run per paid backend, started by JJB |
| 5. Cutover | Experiment policies for Sets B, D and E. Set F on the new stack | Set F confirms. The new stack then becomes the line that ships |

### How it serves the experiment sets

| Set | Question | What the Dojo must produce |
| --- | --- | --- |
| A | Which decision model reviews the sorter best? | Class accuracy, silent-wrong per 1,000, park rate, cost per review |
| B | Can a decision model gate archives? | Ranking power of each gate for "archive is correct", and the review rate needed at 2% wrong |
| C | Which prompts and worker models extract best? | Paired per-document score differences with intervals, gain per dollar |
| D | Do stronger supervisors pay for themselves? | Utility difference against noise floor, oversight cost against review cost saved |
| E | Cheapest safe handling of mergers? | Cost per document, clean-subset MAUD answers correct |
| F | Confirmation and client evidence | Risk-coverage per class with bounds, dollars per 1,000 documents, and the volume projections |

Sets A, the offline half of B, and C can run on the current stack as soon as money is released. Stage 2 re-scores them. Sets that compare routing (the live half of B, D, E) are cleaner on the new engine, where each arm is one policy file.

### This is now several projects

The scope has grown since the first draft. It is one design, but it should be planned and built as separate projects in this order, each with its own plan and gate:

1. Record and store.
2. Labels package, including the EDA move.
3. Dojo.
4. Prompt optimization and the merger work. Both need only the record, labels and Dojo, so they can run on the current pipeline and do not wait for the engine.
5. Engine, after the go or no-go gate.
6. Launcher, backends and cutover.

## Systems review

This section stress-tests the decisions above the way a production system would be reviewed. It changes seven of them and adds one go or no-go gate. The single largest risk is not technical: it is rewriting a working pipeline against a deadline.

### Requirements, stated as numbers

| Requirement | Target | Basis |
| --- | --- | --- |
| Wrong-archive rate | At most 2% of scorable archives, per class | Budget plan bar |
| Durability | No accepted document is ever lost. A crash loses at most the step in flight | JJB's lost-records pain |
| Audit | Any decision can be reconstructed from the record alone, and tampering is detectable | Carried through |
| Throughput | At least 249 documents per minute with the fake model | PR #297 benchmark |
| Re-scoring | Any stored run re-scored in minutes on a laptop, with no model call | Experiment loop |
| Recovery | A stopped run resumes without repeating a paid model call | Cost and determinism |
| Team | One owner plus one collaborator, with agents doing most of the building | Sets the complexity budget |

### Load estimate

These are Claude's estimates from v1 and the budget plan, at an assumed 10,000 documents a day. The real client volume is still unknown.

| Quantity | Estimate | How |
| --- | --- | --- |
| Tokens per document | About 70,000 on the slice mix | 216 million tokens over 3,064 documents |
| Tokens per day | About 700 million, or 8,100 a second sustained | 10,000 documents |
| Model cost per day | About $63 on Qwen flash | $0.09 per million tokens |
| Model calls per document | About 5 | 127 calls over 25 documents in the v1 baseline |
| Events per day | About 200,000 | Roughly 20 events per document including handoffs |
| Event storage per day | About 200 MB raw, a fraction of that sealed | About 1 KB per event |

**What this says:** the data is small. A single SQLite file handles this many times over. The limit on throughput is the model provider's rate limit, not the database. Today's slow databases come from lock design, not volume, so the fix is fewer locks and fewer writes, not a bigger database. Do not add a message broker, a warehouse or a cluster.

### Changes this review makes

| Decision | Problem found | Change |
| --- | --- | --- |
| Event log plus live index | Two writes can disagree after a crash | The log is the only truth. The index is derived and can be rebuilt from the log with one command. Nothing is written to the index that is not already in the log |
| Blobs and events | An event could point at a blob that never reached disk | Write order is fixed: blob first, then the event that names it, then the index. Events are flushed in small groups |
| Resume | Replaying a step after a crash would pay for the model call twice and could give a different answer | Each model response is recorded before the engine acts on it. Resume skips any step whose result is in the record. Accepted: a crash after the provider finished a call but before its response was recorded loses that response, and resume repeats the call (paid twice, possibly another answer). No provider idempotency key is relied on; the exposure is one call per step in flight |
| One executor checkpoint | LangGraph has its own checkpointer, which would be a second source of state | The record is the only checkpoint. The framework's own persistence stays off |
| Run hash at seal | A production service never ends, so its run would never seal | Seal on a fixed period (for example hourly) and chain each seal to the one before. Accepted for project 1: the latest seal is not anchored outside the run directory, so deleting it together with the events after the seal before it leaves a valid shorter record (a rollback only an external copy of the seal hash can show; ADR-0002) |
| Hash chain and stored documents | A client may require a document to be deleted, but an append-only chain cannot drop entries | The chain holds only hashes. Content lives in blobs. Deleting a blob leaves a tombstone and the chain still verifies. Blobs are encrypted at rest in a deploy |
| Projections | Five points is too few to trust a fitted curve, and per-class data today is 5 documents | Resampling measured documents is the primary method. A fitted curve is reported only with at least 30 documents in the class, and always beside the resampled figure |

### Failure handling

| Failure | Behavior |
| --- | --- |
| Provider down or rate-limited | A breaker per provider opens. Documents wait in the queue. They are never parked in review because of an outage, which is what happens today |
| Worker dies mid-document | Its claim is a lease with a heartbeat. The lease expires and another worker resumes from the record |
| Same document uploaded twice | One claim per content hash and policy. The second upload links to the first result |
| A document that always fails | After a set number of attempts it moves to a dead-letter state with its cause, visible in the review queue |
| Spend runs away | The cap is checked before each call from the record's running total. The run stops, it does not degrade |
| Record schema changes | Every event carries a schema version. Old events are read through upgrade functions. Stored files are never rewritten |
| Text returned by a model or a plug-in | Treated as untrusted. Handoffs carry typed fields only. Logs carry our own tokens, never a model's free text |

### Package boundaries

The dependency direction is enforced by a test: the Dojo and the engine import the record package and nothing else of ours, the launcher imports all three, and the Dojo never imports the engine. That is what keeps the Dojo installable alone and stops the bloat from growing back.

### The go or no-go gate

Projects 1 to 3 (record, labels, Dojo) are worth having even if the engine is never rewritten, because the record importer, the labels and the Dojo work on the current pipeline's runs. The engine's `legacy` match is the riskiest project: the current graph is about 8,900 lines with years of edge cases.

**Gate after project 3 (the Dojo) and its proof gates, before project 5 (the engine):** JJB decides whether to build the new engine or fall back to approach 3 and slim the current pipeline in place. The engine stage is time-boxed. If router replay is not matching within the box, the fallback is taken and nothing built so far is wasted.

### Trade-offs accepted

| Choice | Gives | Costs | Revisit when |
| --- | --- | --- | --- |
| Single node, SQLite, local files | Simple, fast, nothing to operate | No multi-host scale | More than one host must write the same run |
| LangGraph as executor | Graph view, team familiarity | A framework between policy and execution | Its scheduling limits throughput or blocks exact replay |
| One workspace | No pins, one CI | Breaks repo ownership, downstream repos frozen | A downstream repo must move |
| Record as the only state | Exact replay, one truth | Every feature must fit the event model | Event volume makes resume slow |
| Running both stacks for a while | The deploy is never at risk | Two codebases to keep straight | Set F confirms, then the old line is frozen |

## Calls, open questions and what is left to the builder

### Calls Claude made

JJB did not decide these. Each can be overturned.

| Call | Reason |
| --- | --- |
| LangGraph runs the compiled policy graph | It is what the pipeline uses today and it gives graph viewing for free. The engine owns the graph definition, so this can change later |
| Clean break for the Dojo: new major version, no compatibility layer | Other repos pin a commit, so nothing breaks for them until they move |
| SQLite or Postgres for the live index, column files with an embedded query engine for analysis, compressed content-addressed blobs | Separates the fast path from analysis and stores each document once |
| One hash chain per document, combined into a run hash at seal | Removes today's global chain lock while keeping the audit property |
| Fits chosen by information criterion and leave-one-out error, five-point minimum, warning past three times the measured range | Keeps projections honest on small samples |
| The autoresearch loop stays in the launcher and its scoring moves to the Dojo | A driver is not a scorer |
| The keep list under Constraints | Shown to JJB without objection, but not explicitly approved |
| The launcher is a fourth package in the new workspace | One version across everything that writes or reads a record |

A fresh reviewer read this design as its builder would. These further calls answer what they asked.

| Question | Call |
| --- | --- |
| Which commit is "today" for `legacy`? | The 0.30.0 release tag, after #309 and #310 land. Later 0.30 fixes are ported only by an explicit list |
| The stored runs predate #309. How are its checks proven? | By the mock fixture set and by one new recorded run on the 0.30.0 tag with every routing input captured. JJB starts that run; it is about 25 documents |
| What if stored runs lack inputs the router reads? | Stage 1 audits each stored folder first and reports what routing inputs are present. Router replay covers what is present, and the new recorded run covers the rest |
| Which numbers are authoritative? | Field scores: the Dojo commit pinned at the 0.30.0 tag. Outcomes and utility: the sandbox's stored numbers. They are different layers, so they do not collide. PR #182 is ported after it merges |
| What happens to the old deploy's data at cutover? | A fresh start. The old deploy stays readable for history, and its repos take fixes only. Importing its audit database comes after cutover |
| How is a missing document detected between seals? | Each periodic seal commits the list of documents it covers, so deleting a chain or cutting a tail that a seal covers fails verification. A document added and deleted between two seals, or events appended after the latest seal and removed before the next, are not detectable: no seal names them (the window is the seal period) |
| What is the throughput floor measured with? | Durable per-event writes switched on. SQLite on one host is the design point; Postgres claims are for the deploy |

### Open questions for JJB

1. **Where are the stored run folders?** Partly answered: the v1 runs (25 documents) and the v2 runs (75 documents) are now in the project files under runs/autoresearch, each with its dataset, items, nodes, events and locked spec. The slice-100 r2 run folder is still missing.
2. **Must The-Mailroom visualizer work against the new stack at cutover?** If yes, the trace exporter has to emit the span names it reads today.
3. **What is the workspace called, and is it public from day one?**
4. **What volume, class mix and review cost should the default projection assume?** This is still open from the budget plan.

5. **What does "Set F confirms" mean as a pass rule?** `Approved by JJB on 2026-11-17``: (a) Faithful``: on the same slice-500 draw, the new stack under ``the ``legacy ``policy differs from the old stack by no more than 0.033 in mean utility, and no more documents change outcome between the two stacks than between two identical runs`` of the old stack``. (b) Safe:`` the chosen policy ``on the new stack has a wrong-archive rate no higher than the old-stack control at the same review rate, cost per document within 10%, every document with a complete verified chain, and throughput at the floor. (c) The 2% bar is reported per class as pass or assisted-mode and is not a condition for cutover``.`
6. **Should the merger and label work also land on the current shipping tree?** Answered by JJB: yes, both also land on the current shipping tree.
7. **Is the merger schema in the code today the "designated new" one?** Answered by JJB: the 21-field version 2 schema is current, and it is to be improved along with its prompt.

### Left to the builder

- Exact record schema, file formats and table layouts, within the parts listed above.
- Which current modules survive, by the rule that a gate, report or projection must read their output.
- Package and command names.
- The order of work inside each stage, and the test layout.
- How the policy file is written, as long as one file both draws and runs the graph and its hash is stored on the run.

# mailroom-dojo

Workspace for the lean Mailroom rewrite. Each package lives under `packages/`:
`packages/record/` (`mailroom_record`: the append-only, hash-chained run record and its
blob store) and `packages/labels/` (`mailroom_labels`: label snapshots, precedence, the
quality gate and coverage). Design and build plan: `docs/specs/` and `docs/plans/`.

```bash
python3 -m venv .venv && .venv/bin/pip install -e "packages/record[dev,analysis]" -e "packages/labels[dev]"
.venv/bin/python -m pytest packages/record/tests packages/labels/tests -q
```

## Layout

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

Folders marked with a later project do not exist yet. Requirements behind this layout: `docs/specs/2026-11-18-lean-rewrite-design.md`, R9.

Commit messages follow `.gitmessage` (`git config commit.template .gitmessage`).

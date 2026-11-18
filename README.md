# mailroom-dojo

Workspace for the lean Mailroom rewrite. Each package lives under `packages/`:
`packages/record/` (`mailroom_record`: the append-only, hash-chained run record and its
blob store) and `packages/labels/` (`mailroom_labels`: label snapshots, precedence, the
quality gate and coverage). Design and build plan: `docs/specs/` and `docs/plans/`.

Requires Python 3.11 or later (CI runs 3.11 and 3.12). If `python3` is older, name a
newer interpreter instead, for example `python3.12 -m venv .venv`.

```bash
python3 -m venv .venv && .venv/bin/pip install -e "packages/record[dev,analysis]" -e "packages/labels[dev]"
.venv/bin/python -m pytest packages/record/tests packages/labels/tests -q
```

## Layout

```
mailroom-dojo/
├── README.md                  what this is, the layout, the test command
├── Makefile                   every dev command: install, test, lint, typecheck, check (`make help`)
├── packages/                  one folder per package; each is installable alone
│   ├── common/                mailroom_common: shared foundation (errors, hashing, IO, config, logging)
│   ├── dictionaries/          mailroom_dictionaries: versioned vocabularies and the glossary
│   ├── record/                mailroom_record: run record, blob store, index, verify, replay (project 1)
│   ├── labels/                mailroom_labels: label snapshots and coverage (project 2)
│   ├── dojo/                  mailroom_dojo: scoring, comparison, reports (project 3)
│   ├── engine/                mailroom_engine: agents, handoffs, policies, API (project 5)
│   └── launcher/              mailroom_launcher: run specs, backends, CLI, TUI, floor (project 6)
│       (each package: pyproject.toml, README.md, src/<module>/, tests/, scripts/)
├── config/                    everything tuned without code (from project 2 on)
│   ├── clients/<client>/      a client profile: document classes, schemas, thresholds, label snapshot
│   │   └── mailroom-dataset/  profile.yaml (from build_profile.py), known-warnings.yaml, hub-revisions.yaml
│   ├── policies/              routing policies (one file each; its hash is stored on every run)
│   └── prompts/<prompt_set>/  prompt sets; variants are overlays on the default set
├── labels/                    README.md, and one committed label snapshot per <snapshot>/ with its manifest (project 2)
├── docs/
│   ├── specs/                 the design (the master plan)
│   ├── plans/                 one build plan per project, dated
│   ├── adr/                   architecture decision records
│   ├── test-strategy-*.md     one test strategy per package
│   ├── handoff/               dated notes handing a chunk of work to the next session
│   └── reports/               gate reports, written by scripts
│       └── labels/            label data-quality, coverage and sandbox cross-check reports
├── scripts/                   workspace checks: dependency boundaries, this layout map, session links
├── tests/                     workspace-level tests (the dependency rule, the checks in scripts/)
├── tools/                     governance: the session-link guard and agent-hook tests (`tools/governance/`)
└── .circleci/                 CI (CircleCI only)
```

Rules: dependencies point one way and a test enforces them (`tests/test_workspace_boundaries.py`, table in `scripts/check_boundaries.py`): every package except `record` (for now) may import `common`, which imports nothing of ours; `dictionaries` imports only `common`; `labels`, `dojo` and `engine` may also import `dictionaries`; `labels` imports `record`; `dojo` imports `record` and `labels`; `engine` imports `record` (and reads client profiles from `config/`); `launcher` may import all of them; the Dojo never imports the engine, and `record` imports nothing of ours (adopting `common` is a planned follow-up). Tuning lives in `config/` and `labels/`, never in code. A new folder needs a line in this map. Names say what a thing is, in full words; nesting goes from general to specific.

Folders marked with a later project do not exist yet. Requirements behind this layout: `docs/specs/2026-11-18-lean-rewrite-design.md`, R9. `scripts/check_layout.py` checks this map against the real folders.

## Development

The `Makefile` is the one entry point, used the same way locally and in CI (`make help` lists every target):

```bash
make install     # .venv with every package (dependency order, dev extras) and the dev tools
make test-fast   # pytest -x -q -n auto over every package and tests/
make check       # every gate: lint, typecheck, spell, boundaries, layout, session-links, test
make hooks       # install the pre-commit hooks (ruff, codespell, file hygiene, no session links)
```

`make session-links` checks commit messages only when `origin/main` resolves (it notes the skip otherwise); CI's `no-session-links` job fetches `origin/main` and runs the same script with `--require-base`, so there the check cannot be skipped. `make format` applies ruff's fixes and formatting. Set `PY` to choose the interpreter that creates the venv, or `VENV` for its location.

Commit messages follow `.gitmessage` (`git config commit.template .gitmessage`).

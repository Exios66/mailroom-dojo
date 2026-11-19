# mailroom-dojo

The workspace for the lean Mailroom rewrite: installable packages built around one
append-only, hash-chained run record. Every package lives under `packages/`:
`packages/record/` (`mailroom_record`: the run record and its blob store),
`packages/labels/` (`mailroom_labels`: label snapshots, precedence, the quality gate and
coverage), and the shared libraries `packages/common/` and `packages/dictionaries/`.

The engine and launcher write records; the Dojo only reads them and is the one place a
run is scored. One-way dependencies keep the Dojo installable alone and stop the old
multi-repo dependency mess from growing back: no commit pins between packages, no vendored
copies, one version, one CI (`AGENTS.md`, section 3).

The design (the master plan) is `docs/specs/2026-11-18-lean-rewrite-design.md`. Each
project has a dated build plan in `docs/plans/`.

## Quick start

You need Python 3.11 or later. CI tests 3.11, 3.12 and 3.13.

```bash
make install   # create .venv with every package and the dev tools
make test      # run every package's tests and the workspace tests
```

If `python3` is older than 3.11, name a newer interpreter: `make install PY=python3.12`.

To work on one package, install only that package and the packages it needs. For
labels (it needs the record):

```bash
python3 -m venv .venv && .venv/bin/pip install -e "packages/record[dev,analysis]" -e "packages/labels[dev]"
.venv/bin/python -m pytest packages/record/tests packages/labels/tests -q
```

With an older `python3`, name a newer interpreter here too, for example
`python3.12 -m venv .venv`. Each package README gives its own install command, and most
also give a test command.

## Packages

| Package | Module | What it is | Read more |
|:---|:---|:---|:---|
| `common` | `mailroom_common` | Shared foundation: errors, hashing, IO, config, logging | [`packages/common/README.md`](packages/common/README.md) |
| `dictionaries` | `mailroom_dictionaries` | Versioned vocabularies and the glossary | [`packages/dictionaries/README.md`](packages/dictionaries/README.md) |
| `record` | `mailroom_record` | Run record, blob store, index, verify, replay | [`packages/record/README.md`](packages/record/README.md) |
| `labels` | `mailroom_labels` | Label snapshots, precedence, the quality gate and coverage | [`packages/labels/README.md`](packages/labels/README.md) |
| `dojo` | `mailroom_dojo` | Scoring core: the scoring profile, field-type scorers, classification metrics | [`packages/dojo/README.md`](packages/dojo/README.md) |

Later projects add `engine` and `launcher` (see the layout map below).

## Development

The `Makefile` is the one entry point, used the same way locally and in CI. `make help`
lists every target.

| Command | What it does |
|:---|:---|
| `make install` | Create `.venv` with every package (dependency order, dev extras) and the dev tools |
| `make test` | Run every package's tests and the workspace tests |
| `make test-fast` | The same tests with `pytest -x -q -n auto`: stop at the first failure, run in parallel |
| `make check` | Every gate, in CI order: lint, typecheck, spell, boundaries, layout, session-links, test |
| `make lint` | `ruff check` and `ruff format --check` (no changes made) |
| `make format` | Apply ruff's safe fixes and formatting |
| `make typecheck` | `mypy --strict` over `common`, `dictionaries`, `scripts/` and `tests/` (`[tool.mypy]` in `pyproject.toml`) |
| `make spell` | `codespell` over the tracked text (config in `pyproject.toml`) |
| `make boundaries` | The one-way dependency rule between packages |
| `make layout` | This README's layout map against the real folders |
| `make session-links` | No session links in tracked files or in commit messages since `origin/main` |
| `make hooks` | Install the pre-commit and commit-msg hooks (ruff, codespell, file hygiene, no session links) |

Two variables change where the venv comes from: `PY` chooses the interpreter that creates
it, and `VENV` its location.

`make session-links` checks commit messages only when `origin/main` resolves; otherwise it
notes the skip. CI's `no-session-links` job fetches `origin/main` and runs the same script
with `--require-base`, so there the check cannot be skipped.

## Layout

```
mailroom-dojo/
├── README.md                  what this is, the quick start, the layout map
├── AGENTS.md                  the long-form manual for agents and contributors
├── CLAUDE.md                  the short rules Claude Code loads every session
├── Makefile                   every dev command: install, test, lint, typecheck, check (`make help`)
├── pyproject.toml             workspace settings for pytest, ruff, mypy and codespell
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
│   │   └── mailroom-dataset/  profile.yaml (from build_profile.py), known-warnings.yaml, hub-revisions.yaml, scoring.yaml (the Dojo's scoring profile)
│   ├── policies/              routing policies (one file each; its hash is stored on every run)
│   └── prompts/<prompt_set>/  prompt sets; variants are overlays on the default set
├── labels/                    README.md, and one committed label snapshot per <snapshot>/ with its manifest (project 2)
├── docs/
│   ├── specs/                 the design (the master plan)
│   ├── plans/                 one build plan per project, dated
│   ├── adr/                   architecture decision records
│   ├── test-strategy-*.md     a package's test strategy, where it has one
│   ├── glossary.md            the glossary, generated from mailroom_dictionaries
│   ├── handoff/               dated notes handing a chunk of work to the next session
│   └── reports/               gate reports, written by scripts
│       └── labels/            label data-quality, coverage and sandbox cross-check reports
├── scripts/                   workspace checks: dependency boundaries, this layout map, session links, dictionary sources
├── tests/                     workspace-level tests (the dependency rule, hashing parity, the checks in scripts/)
├── tools/                     governance: the session-link guard and agent-hook tests (`tools/governance/`)
├── .circleci/                 CI (CircleCI only) and the full session-link guard script
├── .claude/                   agent rules, subagents, hooks and settings
└── .github/                   PR templates and the contributing guide
```

`scripts/check_layout.py` (`make layout`) checks this map against the real folders.
Folders of projects not built yet (the `engine` and `launcher` packages,
`config/policies/` and `config/prompts/`) do not exist yet. The requirements behind this layout
are R9 in `docs/specs/2026-11-18-lean-rewrite-design.md`.

Layout rules:

- A new folder needs a line in this map.
- Names say what a thing is, in full words.
- Nesting goes from general to specific.
- Tuning lives in `config/` and `labels/`, never in code.

## Package boundaries

Dependencies point one way. A test enforces the rule (`tests/test_workspace_boundaries.py`),
and `make boundaries` runs the same check. The table of what may import what is in
`scripts/check_boundaries.py`.

| Package | May import | Notes |
|:---|:---|:---|
| `common` | nothing of ours | Every package except `record` (for now) may import it. |
| `dictionaries` | `common` | |
| `record` | nothing of ours | Adopting `common` is a planned follow-up. |
| `labels` | `common`, `dictionaries`, `record` | |
| `dojo` | `common`, `dictionaries`, `record`, `labels` | The Dojo never imports the `engine`. |
| `engine` | `common`, `dictionaries`, `record` | Also reads client profiles from `config/`. |
| `launcher` | all of them | |

## Commits

Commit messages follow `.gitmessage`. Enable it once per clone:

```bash
git config commit.template .gitmessage
```

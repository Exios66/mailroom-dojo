# ADR-0007: Shared leaf packages: `common` and `dictionaries`

**Status:** Accepted
**Date:** 2026-11-18
**Deciders:** JJB (request: build the shared libraries, tooling and dictionaries that keep the
workspace clean and efficient)

## Context

Four packages are planned on top of the record (labels, Dojo, engine, launcher), and each
needs the same plumbing: canonical hashing, strict YAML and JSONL reading, config loading,
logging and one error family. The old constellation grew a copy of each per repo, and the
copies drifted. Controlled vocabularies (document classes, field types, decision causes) were
defined three times (taxonomy, Dojo registry, sandbox), so the same word could mean different
things in different places. The design (R4, R9) asks for one-way dependencies and tuning as
data, not code.

## Decision

- Two **leaf packages** sit under every other package:
  - `packages/common/` (`mailroom_common`): the shared foundation (errors, hashing, IO, config,
    logging and the like). It imports nothing of ours.
  - `packages/dictionaries/` (`mailroom_dictionaries`): versioned vocabularies, taxonomies and
    the glossary, with a validating loader. It imports only `common`.
- **Dependency direction:** every package may import `common` (`record` once it adopts it;
  see Consequences); `labels`, `dojo` and `engine`
  may also import `dictionaries`; neither leaf imports any other package. The table lives in
  `scripts/check_boundaries.py` and `tests/test_workspace_boundaries.py` enforces it; a package
  folder with no row in the table fails the test.
- **`common` is domain-free:** no document classes, node names or client names. Anything that
  names the domain belongs in `dictionaries` (as data) or in the package that owns it.
- **Vocabularies are data:** YAML files with a version, loaded and validated by
  `dictionaries`, and locked by version so a run records which vocabulary it used: the
  manifest pins each dictionary's version and content hash (and whether it was shipped or a
  client override). An old version is recovered from git history by looking up that hash;
  no API loads a past version.
- **Client extensions add dictionaries, not code:** a client that needs another class or
  term ships a dictionary file in its profile; no package changes.

## Options considered

### A. Two leaf packages (chosen)
**Pros:** one copy of the plumbing; vocabularies versioned on their own; the dependency rule
stays a short table. **Cons:** two more packages to install and release.

### B. One repo-wide `utils` module
**Pros:** nothing extra to install. **Cons:** grows into a grab bag with no owner; mixes
domain words with plumbing, so every package depends on everything in it; cannot be versioned
apart from the code that uses it.

### C. Vocabularies inside `labels`
**Pros:** labels are the first user of class names and field types. **Cons:** the engine
would have to import `labels` (or copy the words) to route on classes, which breaks the
rule that the engine does not depend on labels; vocabulary versions would move with label
snapshots.

## Consequences

- Easier: one hashing, one YAML loader and one error family for every package; a new word is
  a data change with a version bump.
- Harder: changing a public name in `common` touches every package that uses it, so its API
  stays small.
- `record` keeps its own hashing and imports nothing of ours today. Adopting `common` is a
  planned follow-up; it changes record's row in the boundary table to allow `common`.
- Revisit when: a package needs something from `common` that names the domain (it belongs in
  `dictionaries` or in that package instead).

## Action items

1. [ ] Build `mailroom_common` and `mailroom_dictionaries` with their tests.
2. [ ] Move `record` onto `common` (hashing, errors) and update its boundary row.

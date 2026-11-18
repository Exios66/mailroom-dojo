# ADR-0006: Importing sandbox run folders

**Status:** Accepted
**Date:** 2026-11-18
**Deciders:** JJB (design approval: the new Dojo must read the run folders the current
sandbox writes)

## Context

The new Dojo is proved by re-scoring stored runs, and the current tree keeps shipping while
the rewrite is built, so sandbox run folders keep being produced. Those folders hold full
document text, labels, per-node decisions and the sandbox's own scores, but not per-call
tokens, per-attempt extractions or decision-slot outputs.

## Decision

- `import_sandbox_run` turns one folder into one record with `origin="imported_sandbox"`,
  sealed at the end. `doc_id` is the document's content SHA-256; the text becomes an
  `original` blob.
- A field the folder did not capture is stored as `None` and listed in `absent_fields`; it is
  never guessed or derived from another field.
- Labels (`expected_*`) are not imported; they belong to the labels package (project 2). Only
  `expected_doc_class` is kept as `draw_class` so the draw can be described.
- The sandbox's own score dict is kept under `payload.legacy_score` so the new Dojo can check
  its numbers against it. Free text from models (`options` text in decisions) is dropped.
- Importing the same folder twice is a no-op that reports `already_imported`.
- `audit_sandbox_run` reports which routing inputs the engine's `legacy` replay will need are
  present or absent, per folder.

## Options considered

### A. Import into records (chosen)
**Pros:** one reader for every run; dedup of documents across runs. **Cons:** an importer to
maintain while the sandbox format moves.

### B. A Dojo adapter that reads sandbox folders directly
**Pros:** no import step. **Cons:** two readers forever, no verification, no dedup.

## Consequences

- Easier: the Dojo reads only records; stored runs become verifiable.
- Harder: each sandbox format change needs an importer change (fixtures pin the format).
- Revisit when: the sandbox is replaced by the launcher (project 6).

## Action items

1. [ ] Task 10: importer, audit, fixtures with synthetic text.
2. [ ] Task 12: import and verify all stored v1/v2 runs.

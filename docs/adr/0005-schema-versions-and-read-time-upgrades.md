# ADR-0005: Schema versions and read-time upgrades

**Status:** Accepted
**Date:** 2026-11-18
**Deciders:** JJB (design approval, failure-handling table)

## Context

Stored records must stay valid for years and across code versions. Rewriting stored files to
migrate them would change hashes and break every chain and seal.

## Decision

Every `Run`, `Event` and `Seal` carries `schema_version`. Stored bytes are never rewritten.
On read, `upgrade_event` and `upgrade_run` apply a table of `{from_version: function}` steps
in order until the current version; a version newer than the code raises
`UnknownSchemaVersion`. Hashes are always computed over the object as it was written: verify
hashes the stored (un-upgraded) form, so an upgrade never breaks a chain. Version 0 exists only
to prove the path.

## Options considered

### A. Read-time upgrades, never rewrite (chosen)
**Pros:** chains and seals stay valid forever; old packs still unpack. **Cons:** upgrade
functions accumulate; each needs a test.

### B. Migrate stored files in place
**Pros:** readers see one version. **Cons:** changes hashes, breaks the audit property.

### C. No versioning, additive fields only
**Pros:** nothing to write now. **Cons:** the first rename or type change has no path.

## Consequences

- Easier: evolving the event model.
- Harder: verify must hash the stored form, not the upgraded model.
- Revisit when: the upgrade chain grows long enough to slow reads (then upgrade at pack time
  into a new pack, keeping the old one).

## Action items

1. [ ] Task 3: `upgrade.py`, version 0 to 1 test, newer-version refusal test.
2. [ ] Task 7: a test that verifies a run holding an older-version event.

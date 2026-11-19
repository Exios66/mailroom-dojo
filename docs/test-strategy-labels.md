# Test strategy: `mailroom_labels` (project 2)

What the labels package must prove and how each claim is tested. The plan is
`docs/plans/2026-11-18-labels-plan.md`; the tests are in `packages/labels/tests/`.

## Real behavior and failure states (R8)

- **Tamper matrix.** For every snapshot file and the manifest: flip one byte, truncate, append a line,
  delete, add an unlisted file, swap two rows, edit the manifest count. Each must raise the exact typed
  error. Positive control: the untouched snapshot loads.
- **Determinism.** Building a snapshot twice from the same source gives byte-identical files and the same
  `manifest_hash`; a one-label change changes exactly one file hash and the `manifest_hash`.
- **Precedence is tested against an oracle.** A tiny, independent, table-driven reference resolver in the
  tests; `hypothesis` generates random layer sets and asserts the real resolver equals the oracle, that
  every resolved status and value comes from some layer, that a populated Hub value is never replaced
  by `gap_fill`, `derived` or `near_gold` (a Hub absence may be replaced by interim `gap_fill` or
  `derived`, never by `near_gold`), that a gold decision always wins, that results do not depend on input order,
  and that resolving twice gives the same result.
- **Failure states are reported, not swallowed.** Malformed JSON line (reported with file and line),
  duplicate (document, field), unknown status, unknown field, wrong type, vocabulary violation,
  document missing from the Hub index, content-hash mismatch, decision aimed at nothing, conflicting
  decisions: each has a test asserting the finding kind and that valid neighbours still load.
- **Real data golden checks** (no network, committed snapshots only): document and label counts per layer
  and class; zero quality errors; the coverage table; the known facts from the review (1,038 of the 1,039
  correspondence audit decisions equal the near-gold catalog value, one differs: DOC-672cec2c85c83c39
  action_items); the 8 human desk decisions resolve to `gold`.
- **Parity with today's behavior.** A test restates the sandbox rules (a populated Hub value wins over `gap_fill`,
  `derived` and `near_gold`, a Hub absence yields to `gap_fill` and `derived`; `correction` replaces; document hash mismatch leaves the document untouched) and runs
  them on fixtures.
- **Boundary.** `mailroom_labels` imports only `mailroom_record` among workspace packages; `record`
  still imports none.
- **Mutation check.** The reviewer applies at least five source mutations (skip the hash check, swap two
  precedence ranks, drop the duplicate check, treat `disputed` as truth, ignore vocabulary) and every
  one must fail a test.

## Additions from the pre-PR reviews

- **Stored manifest metadata is read-only.** A loaded manifest's `source` and
  `known_warnings` are copies on every read, and the stored values are deep read-only, so no
  edit can make a reader see a manifest its hash does not pin; the manifest still copies,
  pickles and dumps the bytes it was read from (`test_snapshot.py`).
- **Reports carry our own tokens.** A synthetic set with a sentinel string in every value
  the report could print, and the report must not contain it (`test_labels_report.py`).
- **Bad source rows are rejected, never a crash.** An overlay or review row nested deeper
  than the reader allows, or holding a lone surrogate, lands in `source.rejected_lines`
  (`test_importers.py`); so does a source number that overflows a float (`1e999`) and a
  review line whose `doc_id` or `field` is not a name, which would otherwise group with the
  string it prints as and supersede a valid decision (`test_hardening.py`).
- **Dumps do not depend on the hash seed.** Set-valued fields dump the same bytes in
  subprocesses under eight `PYTHONHASHSEED` values (`test_labelset.py`).

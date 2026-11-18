# labels

Committed label snapshots for the `mailroom-dataset` client. Each subdirectory is one
snapshot: `.jsonl` data files plus `manifest.json` (sha256, row count and size of every
file, the source, the builder, accepted warnings and `manifest_hash`). Loading verifies
the manifest before reading a row, so an edited file is refused. Never edit a snapshot by
hand; rebuild it with its importer (`packages/labels/README.md`, "Rebuild the committed
snapshots") and check it with `--check`.

| Snapshot | Kind | Source | Files | Rows | Size | Status words it gives |
| --- | --- | --- | --- | --- | --- | --- |
| `hub-ccd040f6/` | `hub` | Hugging Face `Lucius-Morningstar/mailroom-dataset`, `ground_truth` config at revision `ccd040f6a1b6b414bdefd161cdc622b68f6dacf8`, every shard checked against the dataset's `manifest.txt` and every document's text against `content_sha256` | 1 | 3,302 | 8.6 MB | `hub` |
| `interim-2026-11-01/` | `interim` | sandbox `Exios66/local-mailroom-sandbox` `data/labels/interim` at `93f298c2c03c58d36c5ef15874736c6ea211d9ab`, checked against its `MANIFEST.json` | 5 | 1,111 | 0.8 MB | `interim` (`disputed` never truth) |
| `near-gold-2026-11-17/` | `near_gold` | same sandbox commit, `data/labels/gold-overlay` | 3 | 953 | 2.7 MB | `near_gold` |
| `gold-review-2026-11-18/` | `review` | review decisions (Claude Opus audit of the correspondence catalog, owner decisions, missing-field entries; project files) and the sandbox review desk log at the same commit | 1 | 1,050 | 0.4 MB | `gold` (human desk), `near_gold` (Claude audit) |

Use them together: `load_label_set` over all four directories with
`config/clients/mailroom-dataset/profile.yaml` resolves one label per document field by
the profile's precedence (`review:gold`, `interim:correction`, `hub`, `review:near_gold`,
`interim:gap_fill`, `interim:derived`, `hub:absent`, `near_gold`). A run records the set
as `label_snapshot_ids()` (`<snapshot_id>@<manifest_hash>` per snapshot).

```bash
mailroom-labels verify --strict --profile config/clients/mailroom-dataset/profile.yaml \
    labels/hub-ccd040f6 labels/interim-2026-11-01 labels/near-gold-2026-11-17 labels/gold-review-2026-11-18
```

The set verifies strictly with 0 errors and 13 warnings, each accepted with a reason in
the manifest of the snapshot that raises it (`config/clients/mailroom-dataset/known-warnings.yaml`): 7 decisions aimed at a
label no layer holds and 6 interim labels for documents the Hub re-filed under another
subclass. Counts, coverage and the sandbox cross-check are in
`docs/reports/labels/data-quality-2026-11-18.md` and `coverage-2026-11-18.md`;
`packages/labels/tests/test_real_snapshots.py` pins them.

The snapshots hold label values and evidence excerpts quoted from the documents; full document
text only by hash. Label values are field values as the sources state them, and some are long
verbatim excerpts (CUAD clause labels in the Hub snapshot, for example); near-gold evidence
excerpts run up to about 1,000 characters.

To add a snapshot: run the importer into a new directory named `<kind>-<date or revision>`,
verify the set, regenerate the reports, update the golden counts for the intended change,
and commit the snapshot on its own.

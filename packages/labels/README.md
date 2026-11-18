# mailroom-labels

`mailroom_labels`: every label the benchmark scores against, as versioned, hash-checked
snapshots that load with no network; the precedence that turns label layers into one
effective label per document field; the data-quality gate; and the coverage report.
Plan: `docs/plans/2026-11-18-labels-plan.md`. Requirements served: R4, R6, R8, R9.

Install `packages/record` from this workspace first (editable). The labels package
depends on the bare name `mailroom-record`, which pip would otherwise try to resolve on
PyPI, where no such package of ours is published.

```bash
.venv/bin/pip install -e packages/record -e "packages/labels[dev]"
.venv/bin/python -m pytest packages/labels/tests -q
mailroom-labels verify [--strict] --profile config/clients/<client>/profile.yaml labels/<snapshot>...
mailroom-labels coverage --profile ... --json-out c.json --md-out c.md labels/<snapshot>...
mailroom-labels import-hub|import-interim|import-near-gold|import-review --out labels/<snapshot> ... [--check]
```

`verify` exits 0 with no error and 1 with any error; warnings fail it only under
`--strict`, which exits 1 on any warning that the `known_warnings` of the snapshot that
raised it do not accept; `coverage`
exits 1 and writes nothing when verification finds an error; an importer exits 1 when it
refuses its source or, with `--check`, when the fresh build differs from `--out`; usage and
profile errors exit 2. The importers read parquet, so they need the `import` extra
(`pip install -e "packages/labels[import]"`; `dev` includes it).

## Modules

| Module | What it does |
| --- | --- |
| `models.py` | Frozen models: `LayerRecord`, `LayerFact`, `CorpusEntry`, `FieldLabel`, `DocumentLabels`, `Finding`, `SnapshotManifest`; the ranks and status words |
| `errors.py` | Typed errors: `ManifestMissing`, `ManifestMismatch` (with `kind`), `UnknownSnapshotSchema`, `CorruptLine` (file and line), `DuplicateSnapshot`, `ProfileError` |
| `profile.py` | `ClientProfile` and `load_profile`: classes, fields, Dojo field types, vocabularies, precedence |
| `snapshot.py` | `write_snapshot`, `build_manifest`, `load_snapshot` (verifies before reading), `load_snapshot_set`, `snapshot_refs`, `label_snapshot_ids`; `LabelSnapshot.rows()` streams the raw native rows |
| `labelset.py` | `load_label_set`, `LabelSet`, `QualityGateFailed`: the set-level surface a scorer uses |
| `layers.py` | Readers for the four native snapshot kinds (`hub`, `interim`, `near_gold`, `review`) and read-time upgrades |
| `resolve.py` | `resolve`: the precedence, staleness and scorability rules |
| `quality.py` | `check`, `check_dirs`, `verify`: typed findings for every data-quality check |
| `findings.py` | Finding kinds and their severities |
| `coverage.py` | Scorable documents and fields per class, by status and rank; JSON and Markdown |
| `importers/` | `hub.py`, `interim.py`, `near_gold.py` (both on `overlay.py`), `review.py`, `common.py`: pure, deterministic builders from files already on disk |
| `cli.py` | `mailroom-labels verify`, `coverage` and the four `import-*` commands |

Scripts (`scripts/`): `fetch_hub.py` downloads pinned Hub files and verifies each against
the dataset's `manifest.txt`, deleting any download that fails or does not match (the only
code here that touches the network);
`build_profile.py` writes the client profile from the dataset's EDA schemas and the Hub
(`--check` compares); `labels_report.py` writes the data-quality and coverage reports in
`docs/reports/labels/`.

## Layers, ranks and statuses

| Layer | Native shape (`layer_schema`) | Ranks it emits | Status word |
| --- | --- | --- | --- |
| `hub` | `mailroom-labels/hub/v1`: one row per dataset document, with `fields.<f>.{value, presence}` | `hub` (populated), `hub:absent` (documented absence or `not_applicable`) | `hub` |
| `interim` | `mailroom-interim-labels/v1` (sandbox interim overlay) | `interim:correction`, `interim:gap_fill`, `interim:derived`, `interim:disputed` (never truth) | `interim` |
| `near_gold` | `mailroom-interim-labels/v1` (sandbox near-gold overlay, `basis: near_gold:<how>`) | `near_gold` | `near_gold` |
| `review` | `mailroom-labels/review/v1`: one decision per line | `review:gold` (reviewer model `human`), `review:near_gold` (any other reviewer) | `gold`, `near_gold` |

The Hub has two ranks because the sandbox treats them differently: interim `gap_fill` and
`derived` replace a Hub documented absence or `not_applicable` (`apply_overlay` skips only
a populated Hub field), while a near_gold label never replaces any Hub presence code
(`apply_near_gold`). The mailroom precedence is `review:gold`, `interim:correction`, `hub`,
`review:near_gold`, `interim:gap_fill`, `interim:derived`, `hub:absent`, `near_gold`.

The exact field-by-field shapes are in the `layers.py` docstring. The client profile's
`precedence` orders the ranks (highest first); a rank it leaves out is never truth, and
`interim:disputed` can never be listed. A label whose content hash or class disagrees with
the Hub corpus index is stale: it is never truth, and its document is unverified, so the
document has no scorable fields but does not block any other.

## Snapshots

A snapshot is a directory of `.jsonl` files plus `manifest.json`: every file with its
sha256, row count and byte size, the source, the layer schema, the builder, accepted
warnings, and `manifest_hash` (the canonical hash from `mailroom_record.hashing` of the
manifest without that key). Loading refuses a missing, extra, changed, truncated, grown or
reordered file, a wrong row count and a wrong manifest hash, each with its own
`ManifestMismatch.kind`, before any row is read.

## Importers

Each importer reads files already on disk, never the network, and writes a snapshot
through `build_manifest`, recording its sources (repository, revision or commit, path and
sha256 of every input file) and its builder (`mailroom_labels.importers.<kind>` and
`BUILDER_VERSION`). Building twice from the same inputs gives byte-identical files, so
`--check` rebuilds in memory and compares with the committed snapshot without writing.
Nothing is dropped or repaired silently: every omission, mapping and refusal is counted in
the manifest's `source` (for example `presence_mapped` for `pending_annotation` read as
`unlabeled`, `decoded_json_lists`, `not_imported`).

| Command | Reads | Refuses |
| --- | --- | --- |
| `import-hub` | `manifest.txt` and the `ground_truth` parquet shards from `fetch_hub.py`; with `--default-dir`, the `default` shards for the text check | a revision with no entry in `--pins` (`hub-revisions.yaml`), a `manifest.txt` or shard whose sha256 is not the one pinned for `--revision`, a missing shard, or one not listed in or whose sha256 differs from `manifest.txt`. Documents whose text does not hash to the Hub's `content_sha256` (all of them without `--default-dir`) are listed in `source.text_unverified_documents` |
| `import-interim`, `import-near-gold` | a sandbox overlay directory (`MANIFEST.json` + `.jsonl`) at a named commit | a listed file that is missing or whose sha256 differs from the overlay's `MANIFEST.json` (the overlay manifest records no row counts; the rows read are recorded in the snapshot's `source.files`), or a `.jsonl` file it does not list |
| `import-review` | review decision files and the review desk log | a source with a malformed line or an undecided (`unsure`) desk verdict (see below). A decision replaced by a later or a human one is listed under `superseded` with its reason |

A source the importer cannot take whole is refused (exit 1, nothing written): a line that is
not a JSON object or lacks a required key (`source.rejected_lines`), a Hub row whose
`gt_fields` or `gt_presence` is not an object (`unparseable_gt_fields`), an undecided desk
verdict (`not_imported`). `--allow-rejected` writes the snapshot anyway, for inspection: the
left-out items are listed under those keys and `verify` reports each non-empty one as a
`dropped_source_data` error, so such a snapshot never passes the gate. A UTF-8 byte-order
mark at the start of a source file is skipped, not rejected.

`import-review` keeps one decision per label: a human decision beats a Claude one; among
human decisions the latest `reviewed_at` wins, compared as instants (an ISO 8601 string
with a UTC offset, or null; anything else is a rejected line). Nothing is decided by a
guess: the same instant written two ways is a tie, and an undated human decision cannot be
ordered against a dated one, so a tie or an undated decision with another value is kept
beside the winner and `verify` reports a `conflicting_decision` error (an equal value is
merged).

`--known-warnings` takes `config/clients/<client>/known-warnings.yaml`, keyed by snapshot id;
each entry is `{kind, doc, field, reason}` (plus an optional `file`), needs a `reason`, and
the importer records the snapshot's entries in its manifest. An entry accepts only a
warning raised by its own snapshot (and, with `file`, located in that data file): the same
warning from another snapshot stays unknown and fails `verify --strict`.

### Rebuild the committed snapshots

```bash
python packages/labels/scripts/fetch_hub.py --out <new empty download dir> \
    --revision ccd040f6a1b6b414bdefd161cdc622b68f6dacf8 \
    parquet/ground_truth/train/train-00000-of-00001.parquet parquet/ground_truth/test/test-00000-of-00001.parquet \
    parquet/default/train/train-00000-of-00001.parquet parquet/default/test/test-00000-of-00001.parquet
mailroom-labels import-hub --out labels/hub-ccd040f6 --profile config/clients/mailroom-dataset/profile.yaml \
    --dataset-dir <download dir> --default-dir <download dir> --revision ccd040f6a1b6b414bdefd161cdc622b68f6dacf8 \
    --pins config/clients/mailroom-dataset/hub-revisions.yaml --check
mailroom-labels import-interim --out labels/interim-2026-11-01 --source-dir <sandbox>/data/labels/interim \
    --source-path data/labels/interim --repository Exios66/local-mailroom-sandbox \
    --commit 93f298c2c03c58d36c5ef15874736c6ea211d9ab \
    --known-warnings config/clients/mailroom-dataset/known-warnings.yaml --check
mailroom-labels import-near-gold --out labels/near-gold-2026-11-17 --source-dir <sandbox>/data/labels/gold-overlay \
    --source-path data/labels/gold-overlay --repository Exios66/local-mailroom-sandbox \
    --commit 93f298c2c03c58d36c5ef15874736c6ea211d9ab --check
```

`<sandbox>` is a checkout of the sandbox at that commit. The review snapshot's inputs (path, rows
and sha256 of each decision file and the desk log) are in
`labels/gold-review-2026-11-18/manifest.json` (`source.files`); its decision files are
project files, not in a public repository, so it is rebuilt with `import-review
--decisions-root <dir> --decisions <file>... --desk-root <sandbox> --desk
data/labels/review/human_reviews.jsonl --provenance <json>` against copies whose sha256
matches. Drop
`--check` to rewrite a snapshot in place, then run `mailroom-labels verify` on the set and
`labels_report.py` to refresh the reports; the golden counts in
`tests/test_real_snapshots.py` change only for an intended data change.

## Boundaries

`mailroom_labels` imports `mailroom_record` and no other workspace package
(`tests/test_boundaries.py`). Library code names no class or field: they are profile data.

## Using a label set (what the Dojo codes against)

```python
from mailroom_labels import load_label_set, load_profile

labels = load_label_set(["labels/hub-ccd040f6", "labels/interim-2026-11-01",
                         "labels/near-gold-2026-11-17", "labels/gold-review-2026-11-18"],
                        load_profile("config/clients/mailroom-dataset/profile.yaml"))
labels.snapshot_id          # member ids, sorted, joined with "+"
labels.manifest_hash        # hash_obj of the sorted [snapshot_id, manifest_hash] pairs
for doc in labels.documents():   # resolved DocumentLabels, sorted by document id
    doc.scorable_fields, doc.fields
labels.snapshot_refs()      # [{snapshot_id, kind, manifest_hash}, ...]
labels.label_snapshot_ids() # Run.label_snapshot_ids: ["<snapshot_id>@<manifest_hash>", ...]
```

`load_label_set` verifies every manifest, runs the quality gate and raises
`QualityGateFailed` (with the full report) when it finds an error, so a run can never be
scored against a set that does not pass `mailroom-labels verify`.

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
`docs/reports/labels/` with our own tokens only (a label value appears as its type, size and
a short sha256, never as text); `derive_review_snapshot.py` derives a new review snapshot whose
notes name the reviewing role instead of a person's initials (`--check` compares; see
"Rebuild the committed snapshots" below).

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
`--out` must be missing, empty or an earlier build of the same snapshot id, which is
replaced whole (no file of the old build survives); anything else, an invalid snapshot id
or an unreadable option file is a usage error (exit 2, one line). The new build is written
into a temporary directory beside `--out` and swapped in only once every row is written
and its manifest validates, so a refused import or an error leaves an earlier build exactly
as it was and leaves no temporary directory behind. A crash (the process killed) between the
swap's two renames can leave the earlier build only in the hidden `.<name>.build-*/old`
directory beside `--out`; recover it by hand with `mv .<name>.build-*/old <name>` in that
parent directory (or rebuild), then delete the hidden directory.
Nothing is dropped or repaired silently: every omission, mapping and refusal is counted in
the manifest's `source` (for example `presence_mapped` for `pending_annotation` read as
`unlabeled`, `decoded_json_lists`, `not_imported`).

| Command | Reads | Refuses |
| --- | --- | --- |
| `import-hub` | `manifest.txt` and the `ground_truth` parquet shards from `fetch_hub.py`; with `--default-dir`, the `default` shards for the text check | a revision with no entry in `--pins` (`hub-revisions.yaml`), a `manifest.txt` or shard whose sha256 is not the one pinned for `--revision`, a missing shard, or one not listed in or whose sha256 differs from `manifest.txt`. Documents whose text does not hash to the Hub's `content_sha256`, or have no `doc_text`, are listed in `source.text_unverified_documents`; `source.text_check.run` says whether the check ran, and without `--default-dir` it did not, so `verify` warns `text_unchecked` (unknown unless listed, so `--strict` fails) |
| `import-interim`, `import-near-gold` | a sandbox overlay directory (`MANIFEST.json` + `.jsonl`) at a named commit | a listed file that is missing or whose sha256 differs from the overlay's `MANIFEST.json` (the overlay manifest records no row counts; the rows read are recorded in the snapshot's `source.files`), or a `.jsonl` file it does not list |
| `import-review` | review decision files and the review desk log | a source with a malformed line or a desk decision word it does not know (see below). An `unsure` desk mark is a normal outcome, never a label: it is listed under `source.undecided` (with `replaced` when a later dated human decision settled the label), and it is refused only when it cannot be ordered against a human decision on its label (see below). A decision replaced by a later decision or `unsure` mark, or by a human decision, is listed under `superseded` with its reason |

A source the importer cannot take whole is refused (exit 1, nothing written): a line that is
not a strict JSON object (a repeated key, `NaN`, `Infinity` or `-Infinity` included, or a
float such as `1e999` that overflows), lacks a required key, has a review `doc_id` or `field`
that is not a name, or would not load as a snapshot row (nested more than 64 levels deep, or a
string holding a lone surrogate) (`source.rejected_lines`), a Hub row whose
`gt_fields` or `gt_presence` is not an object (`unparseable_gt_fields`), a desk decision
word other than `keep`, `use_suggestion`, `correct` or `unsure` (`not_imported`), an `unsure` mark
that cannot be ordered against a human decision on its label (`unordered_undecided`). `--allow-rejected` writes the snapshot anyway, for inspection: the
left-out items are listed under those keys and `verify` reports each non-empty one as a
`dropped_source_data` error, so such a snapshot never passes the gate. A UTF-8 byte-order
mark at the start of a source file is skipped, not rejected.

`import-review` keeps one decision per label: a human decision beats a Claude one; among
human decisions the latest `reviewed_at` wins, compared as instants (an ISO 8601 string
with a UTC offset, or null; anything else is a rejected line). Nothing is decided by a
guess: the same instant written two ways is a tie, and an undated human decision cannot be
ordered against a dated one, so a tie or an undated decision with another value is kept
beside the winner and `verify` reports a `conflicting_decision` error (an equal value is
merged). An `unsure` mark counts as a mark: when the latest `unsure` on a label is later
than every human decision on it, those decisions are withdrawn (listed under `superseded`,
never gold) and the label stays undecided, as on the desk. An `unsure` mark that cannot be
ordered against a human decision on its label (either is undated, or the latest mark and
the latest decision are the same instant) is listed under `source.unordered_undecided`,
which refuses the import like a rejected line (`--allow-rejected` writes it, and `verify`
reports a `dropped_source_data` error).

`--known-warnings` takes `config/clients/<client>/known-warnings.yaml`, keyed by snapshot id;
each entry is `{kind, doc, field, reason}` (plus an optional `file`), needs a `reason`
(`doc` and `field` are each a string or null; a malformed entry refuses the import, exit 1), and
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

`<sandbox>` is a checkout of the sandbox at that commit. The committed review snapshot
`labels/gold-review-2026-11-18-r2` is derived from the imported snapshot
`gold-review-2026-11-18` (manifest `cf19afd1b077056bc64d2d666bafb23f2bfe301d1c3f19b64121f7e50895c6bf`,
not kept in the tree): `scripts/derive_review_snapshot.py` copies every row and only replaces
the reviewer's initials at the start of a note with `review desk`; its manifest keeps the
import's `source` and records the derivation under `source.derived_from`. The import's inputs
(path, rows and sha256 of each decision file and the desk log) are in
`labels/gold-review-2026-11-18-r2/manifest.json` (`source.files`, kept from the import); its
decision files are project files, not in a public repository, so the import is rebuilt with
`import-review --decisions-root <dir> --decisions <file>... --desk-root <sandbox> --desk
data/labels/review/human_reviews.jsonl --provenance <json>` against copies whose sha256
matches, then derived (`--source` is that import's output directory, named
`gold-review-2026-11-18`):

```bash
python packages/labels/scripts/derive_review_snapshot.py --source <dir>/gold-review-2026-11-18 \
    --out labels/gold-review-2026-11-18-r2 \
    --known-warnings config/clients/mailroom-dataset/known-warnings.yaml --check
```

A committed snapshot is never rewritten in place: `--check` must report it identical. An intended
data change is written as a new snapshot (a new `--out labels/<new-id>` and a new id), after which
the profile, known warnings and reports are pointed at the new snapshot, `mailroom-labels verify`
runs on the set and `labels_report.py` refreshes the reports; the golden counts in
`tests/test_real_snapshots.py` change only for that intended data change.

## Boundaries

`mailroom_labels` imports `mailroom_record` and no other workspace package
(`tests/test_boundaries.py`). Library code names no class or field: they are profile data.

## Using a label set (what the Dojo codes against)

```python
from mailroom_labels import load_label_set, load_profile

labels = load_label_set(["labels/hub-ccd040f6", "labels/interim-2026-11-01",
                         "labels/near-gold-2026-11-17", "labels/gold-review-2026-11-18-r2"],
                        load_profile("config/clients/mailroom-dataset/profile.yaml"))
labels.snapshot_id          # member ids, sorted, joined with "+"
labels.manifest_hash        # hash_obj of the sorted [snapshot_id, manifest_hash] pairs
for doc in labels.documents():   # resolved DocumentLabels, sorted by document id
    doc.scorable_fields, doc.fields
labels.snapshot_refs()      # [{snapshot_id, kind, manifest_hash}, ...]
labels.label_snapshot_ids() # Run.label_snapshot_ids: ["<snapshot_id>@<manifest_hash>", ...]
```

`documents()` and `document(id)` hand out copies of list and dict label values, so
changing a value you were given never changes the label set; a document's `fields` and
`untrusted` are read-only views and the models are frozen. That is the whole guarantee:
a deliberate write through `__dict__` or `object.__setattr__` is not defended against (a
document with no list or dict value is the stored object itself), and `profile` is not
deep-frozen. A snapshot manifest stores `source` and `known_warnings` deep read-only
(`stored_source`, `stored_known_warnings`: read-only views and tuples) and its `source`
and `known_warnings` hand out fresh copies. A `DocumentLabels` copies (`copy.deepcopy`)
and pickles; a `LabelSet` does neither.

`load_label_set` verifies every manifest, runs the quality gate and raises
`QualityGateFailed` (with the full report) when it finds an error or a warning that the
snapshot raising it does not accept: it is strict by default, the same rule as
`verify --strict`, so a run can never be scored against a set that does not pass
`mailroom-labels verify --strict` (for example a Hub import whose text was not checked,
which only warns `text_unchecked`). `strict=False` loads such a set for inspection, with
the warnings in `report`; a scorer never passes it.

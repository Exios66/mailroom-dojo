"""Download pinned files of the mailroom dataset from the Hugging Face Hub, verified.

This is the only code in the labels package that touches the network. The importers read
the files it leaves on disk and never fetch anything themselves.

It downloads ``manifest.txt`` at the pinned revision first, reads the sha256 the dataset
publishes for every file, then downloads each requested file and refuses (deleting the
partial download, exit 1) any file whose sha256 differs from the manifest or that the
manifest does not list. The output directory must be new or empty, so a stale or planted
file can never be mistaken for a verified one.

    python packages/labels/scripts/fetch_hub.py --out <new empty download dir> \
        --revision ccd040f6a1b6b414bdefd161cdc622b68f6dacf8 \
        parquet/ground_truth/train/train-00000-of-00001.parquet \
        parquet/ground_truth/test/test-00000-of-00001.parquet

With no file arguments it fetches the ground_truth and default parquet shards (the files
``mailroom-labels import-hub`` reads). Exit 0: every file verified; 1: a hash or listing
failure; 2: usage error.
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
import urllib.request
from pathlib import Path, PurePosixPath

from mailroom_labels.importers.hub import DEFAULT_SHARDS, parse_dataset_manifest

REPOSITORY = "Lucius-Morningstar/mailroom-dataset"
REVISION = "ccd040f6a1b6b414bdefd161cdc622b68f6dacf8"
DEFAULT_FILES = DEFAULT_SHARDS
_REVISION = re.compile(r"^[0-9a-f]{40}$")


def _url(repository: str, revision: str, path: str) -> str:
    return f"https://huggingface.co/datasets/{repository}/resolve/{revision}/{path}"


def _download(url: str, target: Path) -> str:
    digest = hashlib.sha256()
    target.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url, timeout=120) as response, target.open("wb") as fh:  # noqa: S310 - https only
        while chunk := response.read(1 << 20):
            digest.update(chunk)
            fh.write(chunk)
    return digest.hexdigest()


def _safe(path: str) -> bool:
    pure = PurePosixPath(path)
    return path != "" and not pure.is_absolute() and ".." not in pure.parts and "\\" not in path


def fetch(out: Path, files: list[str], *, repository: str = REPOSITORY, revision: str = REVISION) -> int:
    """Download and verify ``files`` into ``out``; return a process exit code."""
    if not _REVISION.match(revision):
        print(f"revision must be a 40-hex commit, got {revision!r}", file=sys.stderr)
        return 2
    if out.exists() and any(out.iterdir()):
        print(f"{out} is not empty: give a new or empty directory", file=sys.stderr)
        return 2
    out.mkdir(parents=True, exist_ok=True)
    manifest_path = out / "manifest.txt"
    _download(_url(repository, revision, "manifest.txt"), manifest_path)
    try:
        listed = parse_dataset_manifest(manifest_path.read_text(encoding="utf-8"))
        if not listed:
            raise ValueError("no sha256 block")
    except ValueError as exc:
        print(f"manifest.txt: {exc}", file=sys.stderr)
        return 1
    status = 0
    for path in files:
        if not _safe(path):
            print(f"REFUSED {path}: not a relative dataset path", file=sys.stderr)
            status = 1
            continue
        expected = listed.get(path)
        if expected is None:
            print(f"REFUSED {path}: not listed in manifest.txt", file=sys.stderr)
            status = 1
            continue
        target = out / path
        actual = _download(_url(repository, revision, path), target)
        if actual != expected:
            target.unlink()
            print(f"REFUSED {path}: sha256 {actual} != manifest {expected}", file=sys.stderr)
            status = 1
            continue
        print(f"verified {path} {actual}")
    return status


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, required=True, help="new or empty download directory")
    parser.add_argument("--repository", default=REPOSITORY)
    parser.add_argument("--revision", default=REVISION)
    parser.add_argument("files", nargs="*", default=list(DEFAULT_FILES))
    args = parser.parse_args(argv)
    return fetch(args.out, args.files, repository=args.repository, revision=args.revision)


if __name__ == "__main__":
    sys.exit(main())

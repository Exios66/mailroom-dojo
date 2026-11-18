"""Render the glossary data file to docs/glossary.md.

Usage: python packages/dictionaries/scripts/render_glossary.py [--check] [--glossary FILE] [--output FILE]

With --check, writes nothing and exits 1 when the output file is missing or differs from a
fresh render. Exit 1 also for an invalid glossary file.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from mailroom_common.errors import ConfigError
from mailroom_dictionaries.glossary import GLOSSARY_PATH, load_glossary, render_glossary

DEFAULT_OUTPUT = Path(__file__).resolve().parents[3] / "docs" / "glossary.md"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="fail if the output is stale; write nothing")
    parser.add_argument("--glossary", type=Path, default=GLOSSARY_PATH)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    try:
        text = render_glossary(load_glossary(args.glossary))
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.check:
        try:
            current = args.output.read_text(encoding="utf-8")
        except FileNotFoundError:
            current = None
        if current != text:
            print(
                f"stale: {args.output} does not match {args.glossary}; run render_glossary.py",
                file=sys.stderr,
            )
            return 1
        print(f"{args.output} is current")
        return 0
    args.output.write_text(text, encoding="utf-8")
    print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

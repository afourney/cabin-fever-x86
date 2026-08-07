#!/usr/bin/env python3
"""Apply ROT13 to a UTF-8 text file.

ROT13 is symmetric, so the same command encodes plaintext and decodes an
obfuscated file::

    uv run rot13_file.py hints/zork1.rot13
    uv run rot13_file.py hints/zork1.txt --output hints/zork1.rot13
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from cabin_fever_x86_core.hints import rot13


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("path", type=Path, help="UTF-8 text file to transform.")
    parser.add_argument(
        "--output",
        type=Path,
        help="Write the transformed text here instead of streaming it to stdout.",
    )
    return parser.parse_args()


def main() -> None:
    """Transform the requested file and write it to the selected destination."""
    args = _parse_args()
    with args.path.open("r", encoding="utf-8", newline="") as handle:
        transformed = rot13(handle.read())
    if args.output is None:
        sys.stdout.write(transformed)
    else:
        with args.output.open("w", encoding="utf-8", newline="") as handle:
            handle.write(transformed)


if __name__ == "__main__":
    main()

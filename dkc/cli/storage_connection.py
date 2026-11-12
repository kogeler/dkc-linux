"""Materialize one private storage connection without logging its values."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dkc.storage_connection import materialize_connection


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--provided", type=Path)
    args = parser.parse_args()
    try:
        materialize_connection(args.output, provided=args.provided)
    except BaseException:
        print("FAIL unable to prepare private storage connection", file=sys.stderr)
        return 1
    print("PASS private storage connection prepared")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Resolve the newest authenticated Debian kernel source into bounded output."""

from __future__ import annotations

import argparse
import hashlib
import pathlib
import subprocess
import sys
from datetime import datetime, timezone

from dkc.serialize import dumps
from dkc.source_discovery import build_inventory, make_variables


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=pathlib.Path, required=True)
    parser.add_argument("--epoch", type=int, required=True)
    parser.add_argument("--mirror", default="http://deb.debian.org/debian")
    args = parser.parse_args()
    if args.output.exists() or args.epoch < 1:
        raise SystemExit("invalid or pre-existing source discovery output")
    args.output.mkdir(parents=True)
    captured = subprocess.check_output(
        ["scripts/in-container/sources-index.sh", "sid", "linux"],
        text=True,
        encoding="utf-8",
        stderr=sys.stderr,
        env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "DKC_DEBIAN_MIRROR": args.mirror},
    )
    inventory = build_inventory(
        captured,
        mirror=args.mirror,
        discovered=datetime.fromtimestamp(args.epoch, timezone.utc),
    )
    inventory_path = args.output / "source-inventory.json"
    inventory_path.write_text(dumps(inventory), encoding="utf-8")
    variables = make_variables(inventory)
    (args.output / "source.env").write_text(
        "".join(f"{name}={value}\n" for name, value in sorted(variables.items())),
        encoding="utf-8",
    )
    (args.output / "result.env").write_text(
        "status=PASS\nsource_discovery=PASS\n",
        encoding="utf-8",
    )
    checksums = []
    for path in sorted(args.output.iterdir()):
        checksums.append(f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}")
    (args.output / "evidence.sha256").write_text(
        "\n".join(checksums) + "\n", encoding="utf-8"
    )
    print("PASS authenticated source discovery completed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

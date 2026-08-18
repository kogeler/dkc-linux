#!/usr/bin/env python3
"""Replace dpkg-source's interactive quilt template with reviewed DEP-3 data."""

from __future__ import annotations

import pathlib
import sys


HEADER = """Description: add x86-64-v2, v3, and v4 kernel baselines
 The ordinary kernel build retains its explicit no-SIMD flags after selecting
 each psABI compiler baseline. Debian packaging exposes the three configurations
 as independent flavors.
Origin: vendor
Forwarded: not-needed
Author: DKC Kernel Maintainers <build@dkc.invalid>
Last-Update: 2026-08-13
---
"""


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: normalize-quilt-patch.py <patch>", file=sys.stderr)
        return 2
    path = pathlib.Path(sys.argv[1])
    if not path.is_file() or path.is_symlink():
        raise SystemExit("quilt patch editor input is not a plain file")
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    start = next(
        (
            index
            for index in range(len(lines) - 1)
            if lines[index].startswith("--- ") and lines[index + 1].startswith("+++ ")
        ),
        None,
    )
    if start is None:
        raise SystemExit("generated quilt patch has no unified diff")
    body = "".join(lines[start:])
    if "arch/x86/Kconfig.cpu" not in body or "arch/x86/Makefile" not in body:
        raise SystemExit("generated quilt patch lacks the reviewed x86 paths")
    path.write_text(HEADER + body, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

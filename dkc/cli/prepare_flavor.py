#!/usr/bin/env python3
"""Restrict the three-flavor Debian packaging graph to one matrix flavor.

The committed overlay defines all three product flavors. Each isolated matrix
job selects exactly one before regenerating Debian control files, so jobs share
one verified source inventory without duplicating packages for other flavors.
"""

from __future__ import annotations

import pathlib
import re
import sys
import tomllib

FLAVORS = ("v2", "v3", "v4")
LTO_CONFIG_LINES = {
    "none": {
        "CONFIG_LTO_NONE=y",
        "# CONFIG_LTO_CLANG_FULL is not set",
        "# CONFIG_LTO_CLANG_THIN is not set",
        "CONFIG_DEBUG_INFO_BTF=y",
        "CONFIG_DEBUG_INFO_BTF_MODULES=y",
    },
    "thin": {
        "# CONFIG_LTO_NONE is not set",
        "# CONFIG_LTO_CLANG_FULL is not set",
        "CONFIG_LTO_CLANG_THIN=y",
        "# CONFIG_DEBUG_INFO_BTF is not set",
        "# CONFIG_DEBUG_INFO_BTF_MODULES is not set",
    },
    "full": {
        "# CONFIG_LTO_NONE is not set",
        "CONFIG_LTO_CLANG_FULL=y",
        "# CONFIG_LTO_CLANG_THIN is not set",
        "# CONFIG_DEBUG_INFO_BTF is not set",
        "# CONFIG_DEBUG_INFO_BTF_MODULES is not set",
    },
}


def main() -> int:
    if len(sys.argv) != 4:
        print(
            "usage: prepare-flavor.py <source-root> <v2|v3|v4> <none|thin|full>",
            file=sys.stderr,
        )
        return 2

    source = pathlib.Path(sys.argv[1])
    flavor = sys.argv[2]
    lto_mode = sys.argv[3]
    if flavor not in FLAVORS:
        raise SystemExit(f"unknown flavor {flavor!r}; expected one of {FLAVORS}")
    if lto_mode not in LTO_CONFIG_LINES:
        raise SystemExit("kernel LTO mode must be none, thin, or full")

    path = source / "debian/config/amd64/defines.toml"
    original = path.read_text(encoding="utf-8")
    parsed = tomllib.loads(original)
    names = [item.get("name") for item in parsed.get("flavour", [])]
    expected = [f"{item}-amd64" for item in FLAVORS]
    if names != expected:
        raise SystemExit(
            f"unexpected amd64 flavor inventory {names!r}; expected {expected!r}"
        )

    marker = "[[featureset]]"
    before, separator, after = original.partition(marker)
    if not separator:
        raise SystemExit("amd64 defines has no [[featureset]] boundary")

    selected = f"{flavor}-amd64"
    blocks = re.split(r"(?=^\[\[flavour\]\]$)", before, flags=re.MULTILINE)
    kept: list[str] = []
    for block in blocks:
        if not block.strip():
            kept.append(block)
            continue
        match = re.search(r"^name = '([^']+)'$", block, flags=re.MULTILINE)
        if not match:
            raise SystemExit("flavor block lacks an exact single-quoted name")
        if match.group(1) == selected:
            kept.append(block)

    updated = "".join(kept).rstrip() + "\n\n" + marker + after
    result = tomllib.loads(updated)
    result_names = [item.get("name") for item in result.get("flavour", [])]
    if result_names != [selected]:
        raise SystemExit(f"failed to select {selected}: {result_names!r}")
    if result.get("build", {}).get("enable_signed") is not False:
        raise SystemExit("overlay did not disable Debian's official signing stage")

    fragment = source / f"debian/config/amd64/config.{selected}"
    symbol = f"CONFIG_DKC_X86_64_BASELINE_{flavor.upper()}=y"
    if not fragment.is_file():
        raise SystemExit(f"{selected} config fragment is absent")
    fragment_lines = set(fragment.read_text(encoding="utf-8").splitlines())
    if symbol not in fragment_lines:
        raise SystemExit(f"{selected} config does not select {symbol}")
    if not LTO_CONFIG_LINES[lto_mode].issubset(fragment_lines):
        raise SystemExit(f"{selected} config does not select {lto_mode} LTO policy")

    path.write_text(updated, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

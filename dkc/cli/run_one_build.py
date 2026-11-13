"""Require the exact selected binary package set before a flavor build compiles."""

from __future__ import annotations

import json
import pathlib
import sys


def main() -> None:
    arguments = sys.argv[1:]
    identity_path, flavor, selected_path = arguments
    identity = json.loads(pathlib.Path(identity_path).read_text(encoding="utf-8"))
    names = identity.get("package_names")
    abi = identity.get("abi")
    if not isinstance(names, dict) or not isinstance(abi, str):
        raise SystemExit("malformed package inventory in publication identity")
    versioned = names.get("versioned")
    meta = names.get("meta")
    if not isinstance(versioned, list) or not isinstance(meta, list):
        raise SystemExit("malformed versioned/meta package inventory")
    all_names = versioned + meta
    if not all(isinstance(name, str) for name in all_names):
        raise SystemExit("non-string package in publication identity")
    expected = {name for name in all_names if name.endswith(f"-{flavor}-amd64")}
    expected.update({f"dkc-linux-headers-{abi}-common", f"dkc-linux-kbuild-{abi}"})
    selected_lines = (
        pathlib.Path(selected_path).read_text(encoding="utf-8").splitlines()
    )
    if (
        len(selected_lines) != len(set(selected_lines))
        or set(selected_lines) != expected
    ):
        raise SystemExit(
            "selected binary target set differs before compilation: "
            f"missing={sorted(expected - set(selected_lines))}, "
            f"unexpected={sorted(set(selected_lines) - expected)}"
        )


if __name__ == "__main__":
    main()

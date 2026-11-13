"""Require replayable post-build gate results for recover-flavor-attestation.sh."""

from __future__ import annotations

import pathlib
import sys


def main() -> None:
    arguments = sys.argv[1:]
    values = {}
    for line in pathlib.Path(arguments[0]).read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        if not separator or key in values:
            raise SystemExit("post-build gate result is malformed")
        values[key] = value
    if set(values) != {
        "package_attestation_rc",
        "kbuild_audit_rc",
        "simd_audit_rc",
        "lintian_rc",
    }:
        raise SystemExit("post-build gate set is incomplete")
    if values["lintian_rc"] != "0":
        raise SystemExit("lintian did not pass and cannot be replayed")
    for key in ("package_attestation_rc", "kbuild_audit_rc", "simd_audit_rc"):
        if not values[key].isdigit():
            raise SystemExit(f"invalid gate return code: {key}")


if __name__ == "__main__":
    main()

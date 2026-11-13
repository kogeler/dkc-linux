"""Verify the retained replay inputs for reattest-flavor.sh."""

from __future__ import annotations

import hashlib
import json
import pathlib
import sys


def main() -> None:
    arguments = sys.argv[1:]

    def digest(path: pathlib.Path) -> tuple[int, str]:
        value = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                value.update(chunk)
        return path.stat().st_size, value.hexdigest()

    (
        manifest_path,
        compressed,
        system_map,
        config,
        executable_sections,
        kbuild_commands,
        build_tree_inventory,
        llvm_major,
    ) = arguments
    manifest = json.loads(pathlib.Path(manifest_path).read_text(encoding="utf-8"))
    if (
        manifest.get("schema_version") != 2
        or manifest.get("status") != "COMPLETE"
        or manifest.get("llvm_major") != int(llvm_major)
        or manifest.get("executable_sections_comparison") != "PASS"
    ):
        raise SystemExit("attestation replay manifest identity is invalid")
    for key, name in (
        ("vmlinux_zst", compressed),
        ("system_map", system_map),
        ("config", config),
        ("executable_sections", executable_sections),
        ("kbuild_commands", kbuild_commands),
        ("build_tree_inventory", build_tree_inventory),
    ):
        size, sha256 = digest(pathlib.Path(name))
        if manifest.get(key) != {"size": size, "sha256": sha256}:
            raise SystemExit(f"attestation replay input differs: {key}")


if __name__ == "__main__":
    main()

"""Write the attestation replay manifest for prepare-attestation-replay.sh."""

from __future__ import annotations

import hashlib
import json
import pathlib
import sys


def main() -> None:
    arguments = sys.argv[1:]

    def record(path: pathlib.Path) -> dict[str, object]:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return {"size": path.stat().st_size, "sha256": digest.hexdigest()}

    (
        original,
        replay,
        compressed,
        system_map,
        config,
        executable_sections,
        kbuild_commands,
        build_tree_inventory,
        llvm_major,
        output,
    ) = arguments
    document = {
        "schema_version": 2,
        "status": "COMPLETE",
        "llvm_major": int(llvm_major),
        "transform": f"llvm-objcopy-{llvm_major} --strip-debug",
        "executable_sections_comparison": "PASS",
        "original_vmlinux": record(pathlib.Path(original)),
        "replay_vmlinux": record(pathlib.Path(replay)),
        "vmlinux_zst": record(pathlib.Path(compressed)),
        "system_map": record(pathlib.Path(system_map)),
        "config": record(pathlib.Path(config)),
        "executable_sections": record(pathlib.Path(executable_sections)),
        "kbuild_commands": record(pathlib.Path(kbuild_commands)),
        "build_tree_inventory": record(pathlib.Path(build_tree_inventory)),
    }
    pathlib.Path(output).write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()

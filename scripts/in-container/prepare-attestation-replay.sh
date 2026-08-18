#!/usr/bin/env bash
# Preserve the minimum final-ELF state needed to rerun post-build attestation.

set -Eeuo pipefail

[ "$#" -eq 3 ] || {
	echo "usage: prepare-attestation-replay.sh <build-root> <evidence-root> <llvm-major>" >&2
	exit 2
}

build_root="$1"
evidence="$2"
llvm_major="$3"
[[ "$llvm_major" =~ ^[0-9]+$ ]] || {
	echo "invalid LLVM major" >&2
	exit 2
}

vmlinux="$build_root/vmlinux"
system_map="$build_root/System.map"
config="$build_root/.config"
for input in "$vmlinux" "$system_map" "$config"; do
	test -f "$input" || {
		echo "attestation replay input is absent: $input" >&2
		exit 1
	}
done

objcopy="llvm-objcopy-${llvm_major}"
command -v "$objcopy" >/dev/null
command -v zstd >/dev/null

replay="$evidence/attestation-replay"
test ! -e "$replay" || {
	echo "refusing to replace an existing attestation replay directory" >&2
	exit 1
}
mkdir -p "$replay"
cp "$system_map" "$replay/System.map"
cp "$config" "$replay/config"

# DWARF is not needed to disassemble executable sections or attribute vmlinux
# addresses through System.map.  Removing it keeps the replay artifact small
# while preserving the exact machine code consumed by the SIMD scanner.
"$objcopy" --strip-debug "$vmlinux" "$replay/vmlinux"
zstd -q -T1 -3 -f "$replay/vmlinux" -o "$replay/vmlinux.zst"
python3 /work/repo/scripts/in-container/verify-replay-elf.py \
	"$vmlinux" "$replay/vmlinux" \
	"$replay/executable-sections.json" "$llvm_major"
python3 /work/repo/scripts/in-container/attest-one-build.py \
	--capture-replay "$build_root" "$evidence" "$llvm_major"

python3 - "$vmlinux" "$replay/vmlinux" "$replay/vmlinux.zst" \
	"$replay/System.map" "$replay/config" "$replay/executable-sections.json" \
	"$evidence/kbuild-commands.tsv.xz" \
	"$replay/build-tree-inventory.json" "$llvm_major" \
	"$replay/manifest.json" <<'PY'
import hashlib
import json
import pathlib
import sys


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
) = sys.argv[1:]
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
PY
rm -f -- "$replay/vmlinux"

echo "attestation replay inputs COMPLETE" >&2

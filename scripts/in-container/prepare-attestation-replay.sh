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
PYTHONPATH=/work/repo python3 -m dkc.cli.verify_replay_elf \
	"$vmlinux" "$replay/vmlinux" \
	"$replay/executable-sections.json" "$llvm_major"
PYTHONPATH=/work/repo python3 -m dkc.cli.attest_one_build \
	--capture-replay "$build_root" "$evidence" "$llvm_major"

PYTHONPATH=/work/repo python3 -m dkc.cli.prepare_attestation_replay "$vmlinux" "$replay/vmlinux" "$replay/vmlinux.zst" \
	"$replay/System.map" "$replay/config" "$replay/executable-sections.json" \
	"$evidence/kbuild-commands.tsv.xz" \
	"$replay/build-tree-inventory.json" "$llvm_major" \
	"$replay/manifest.json"
rm -f -- "$replay/vmlinux"

echo "attestation replay inputs COMPLETE" >&2

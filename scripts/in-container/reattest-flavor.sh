#!/usr/bin/env bash
# Replay Kbuild and SIMD attestation from a completed compilation result.

set -Eeuo pipefail

[ "$#" -eq 5 ] || {
	echo "usage: reattest-flavor.sh <result> <output> <llvm-major> <v2|v3|v4> <observations|full>" >&2
	exit 2
}

result="$1"
output="$2"
llvm_major="$3"
flavor="$4"
mode="$5"
[[ "$llvm_major" =~ ^[0-9]+$ ]] || {
	echo "invalid LLVM major" >&2
	exit 2
}
[[ "$flavor" =~ ^v[234]$ ]] || {
	echo "invalid flavor" >&2
	exit 2
}
case "$mode" in observations | full) ;; *)
	echo "invalid replay mode" >&2
	exit 2
	;;
esac

artifacts="$result/artifacts"
evidence="$result/evidence"
replay="$evidence/attestation-replay"
for path in \
	"$artifacts" \
	"$evidence/evidence.sha256" \
	"$evidence/kbuild-commands.tsv.xz" \
	"$evidence/publication-identity.json" \
	"$replay/derived-fpu-symbols.json" \
	"$replay/build-tree-inventory.json" \
	"$replay/executable-sections.json" \
	"$replay/manifest.json" \
	"$replay/System.map" \
	"$replay/vmlinux.zst"; do
	test -e "$path" || {
		echo "replay input is absent: $path" >&2
		exit 1
	}
done
if [ "$mode" = observations ]; then
	test -f "$replay/kernel-simd-observations.json.xz" || {
		echo "replay input is absent: $replay/kernel-simd-observations.json.xz" >&2
		exit 1
	}
fi

(
	cd "$evidence"
	sha256sum --check evidence.sha256 >/dev/null
)

if [ -f "$evidence/artifacts.sha256" ]; then
	artifact_manifest="$evidence/artifacts.sha256"
elif [ -f "$evidence/failure-artifacts.sha256" ]; then
	artifact_manifest="$evidence/failure-artifacts.sha256"
else
	echo "result has no artifact checksum manifest" >&2
	exit 1
fi
(
	cd "$artifacts"
	sha256sum --check "$artifact_manifest" >/dev/null
)

mkdir -p "$output/evidence" /work/replay
PYTHONPATH=/work/repo python3 -m dkc.cli.reattest_flavor "$replay/manifest.json" "$replay/vmlinux.zst" \
	"$replay/System.map" "$replay/config" "$replay/executable-sections.json" \
	"$evidence/kbuild-commands.tsv.xz" "$replay/build-tree-inventory.json" \
	"$llvm_major"

identity="$evidence/publication-identity.json"
lto_mode="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["lto_mode"])' \
	"$identity")"
source_version="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["debian_source_version"])' \
	"$identity")"
profile_dir="$(PYTHONPATH=/work/repo python3 -m dkc.sourceprofile \
	/work/repo "$source_version" directory)"
case "$lto_mode" in none | thin | full) ;; *)
	echo "publication identity has an invalid LTO mode" >&2
	exit 1
	;;
esac

zstd -q -d -c "$replay/vmlinux.zst" >/work/replay/vmlinux
PYTHONPATH=/work/repo python3 -m dkc.cli.attest_one_build \
	/work/replay/vmlinux "$artifacts" "$output/evidence" "$llvm_major" \
	"$identity" "$flavor" "$lto_mode" \
	--replay-evidence "$evidence"

PYTHONPATH=/work/repo python3 -m dkc.cli.audit_kbuild_commands \
	"$output/evidence/kbuild-commands.tsv.xz" \
	"/work/repo/config/flavors/${flavor}.toml" "$profile_dir" \
	"$output/evidence/kbuild-command-audit.json" "$lto_mode"

simd_common=(
	"$artifacts"
	"$profile_dir"
	"$output/evidence/kernel-simd-audit.json"
	"$llvm_major"
	--lto-mode "$lto_mode"
	--derived-fpu-inventory "$replay/derived-fpu-symbols.json"
)
if [ "$mode" = observations ]; then
	PYTHONPATH=/work/repo python3 -m dkc.cli.audit_kernel_simd \
		/dev/null "${simd_common[@]}" \
		--observations-input "$replay/kernel-simd-observations.json.xz"
else
	PYTHONPATH=/work/repo python3 -m dkc.cli.audit_kernel_simd \
		/work/replay/vmlinux "${simd_common[@]}" \
		--system-map "$replay/System.map" \
		--observations-output "$output/evidence/kernel-simd-observations.json.xz"
fi

cat >"$output/evidence/result.env" <<EOF
status=PASS
scope=post-build-reattest
flavor=${flavor}
mode=${mode}
publishable=false
EOF
cp "$evidence/publication-identity.json" "$output/evidence/"
cp "$evidence/evidence.sha256" "$output/evidence/input-evidence.sha256"
cp "$replay/manifest.json" "$output/evidence/replay-input-manifest.json"
(
	cd "$output/evidence"
	find . -type f ! -name evidence.sha256 -print0 |
		sort -z | xargs -0 sha256sum
) >"$output/evidence/evidence.sha256"

echo "post-build reattestation PASS (${mode})" >&2

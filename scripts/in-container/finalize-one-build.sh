#!/usr/bin/env bash
# Stream one accepted flavor build without duplicating its artifacts.

set -Eeuo pipefail

if [ "$#" -ne 1 ] || [[ ! "$1" =~ ^v[234]$ ]]; then
	echo "usage: finalize-one-build.sh <v2|v3|v4>" >&2
	exit 2
fi
flavor="$1"
result="/work/results/${flavor}"

required_evidence=(
	artifacts.sha256
	attestation.json
	attestation-replay/config
	attestation-replay/build-tree-inventory.json
	attestation-replay/derived-fpu-symbols.json
	attestation-replay/executable-sections.json
	attestation-replay/kernel-simd-observations.json.xz
	attestation-replay/manifest.json
	attestation-replay/System.map
	attestation-replay/vmlinux.zst
	build.log.sha256
	build.log.xz
	capacity.env
	config-preflight
	config-preflight.sha256
	kbuild-command-audit.json
	kbuild-commands.tsv.xz
	kernel-simd-audit.json
	lintian.txt
	package-fields.json
	post-build-gates.env
	resources.tsv
	selected-packages.txt
	source-package/source-package.json
	source-package/source-tree.manifest.xz
	source-package/source-tree.manifest.xz.sha256
	time.txt
	tool-minimums.env
)
for name in "${required_evidence[@]}"; do
	test -f "$result/evidence/$name" || {
		echo "missing required build evidence: ${name}" >&2
		exit 1
	}
done
(
	cd "$result/artifacts"
	sha256sum --check ../evidence/artifacts.sha256 >/dev/null
)
expected_log_sha256="$(cut -d ' ' -f1 "$result/evidence/build.log.sha256")"
actual_log_sha256="$(xz --decompress --stdout "$result/evidence/build.log.xz" | sha256sum | cut -d ' ' -f1)"
[ "$actual_log_sha256" = "$expected_log_sha256" ] || {
	echo "compressed build log does not match its uncompressed SHA-256" >&2
	exit 1
}
lto_mode="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["lto_mode"])' \
	/work/inputs/publication-identity.json)"
case "$lto_mode" in
none | thin | full) ;;
*)
	echo "publication identity has an invalid LTO mode" >&2
	exit 1
	;;
esac
source_version="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["debian_source_version"])' \
	/work/inputs/publication-identity.json)"
profile_dir="$(PYTHONPATH=/work/repo python3 -m dkc.sourceprofile \
	/work/repo "$source_version" directory)"
replay_check="$(mktemp /tmp/dkc-simd-replay-XXXXXX.json)"
PYTHONPATH=/work/repo python3 -m dkc.cli.audit_kernel_simd \
	/dev/null "$result/artifacts" \
	"$profile_dir" \
	"$replay_check" "$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["llvm_major"])' \
		"$result/evidence/attestation.json")" \
	--lto-mode "$lto_mode" \
	--derived-fpu-inventory \
	"$result/evidence/attestation-replay/derived-fpu-symbols.json" \
	--observations-input \
	"$result/evidence/attestation-replay/kernel-simd-observations.json.xz" \
	1>&2
cmp "$replay_check" "$result/evidence/kernel-simd-audit.json" || {
	echo "retained SIMD observations do not reproduce the accepted report" >&2
	exit 1
}
rm -f -- "$replay_check"
package_replay="$(mktemp -d /tmp/dkc-package-replay-XXXXXX)"
zstd -q -d -c "$result/evidence/attestation-replay/vmlinux.zst" \
	>"$package_replay/vmlinux"
PYTHONPATH=/work/repo python3 -m dkc.cli.attest_one_build \
	"$package_replay/vmlinux" "$result/artifacts" "$package_replay" \
	"$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["llvm_major"])' \
		"$result/evidence/attestation.json")" \
	/work/inputs/publication-identity.json "$flavor" "$lto_mode" \
	--replay-evidence "$result/evidence" 1>&2
for replayed in attestation.json package-fields.json kbuild-commands.tsv.xz; do
	cmp "$package_replay/$replayed" "$result/evidence/$replayed" || {
		echo "retained inputs do not reproduce package attestation: $replayed" >&2
		exit 1
	}
done
rm -rf -- "$package_replay"
PYTHONPATH=/work/repo python3 -m dkc.cli.finalize_one_build attestation \
	"$result/evidence/attestation.json" \
	"$result/evidence/kbuild-command-audit.json" \
	"$result/evidence/kernel-simd-audit.json" \
	"$result/evidence/attestation-replay/manifest.json" \
	"$result/evidence/post-build-gates.env" \
	/work/inputs/publication-identity.json "$flavor"

PYTHONPATH=/work/repo python3 -m dkc.cli.finalize_one_build source-package "$result/evidence/source-package/source-package.json" \
	"$result/evidence/source-package/source-tree.manifest.xz" \
	/work/source-package /work/inputs/publication-identity.json

cp /work/inputs/source-inventory.json "$result/evidence/"
cp /work/inputs/toolchain.env "$result/evidence/"
cp /work/inputs/build-image-packages.tsv "$result/evidence/"
cp /work/inputs/apt-indexes.sha256 "$result/evidence/"
cp /work/inputs/build-image-debs.tsv "$result/evidence/"
cp /work/inputs/staging-apt-indexes.sha256 "$result/evidence/"
cp /work/inputs/repository-inputs.sha256 "$result/evidence/"
cp /work/inputs/publication-identity.json "$result/evidence/"
cp /work/inputs/policy-config-v2.json "$result/evidence/"
cp /work/inputs/policy-config-v3.json "$result/evidence/"
cp /work/inputs/policy-config-v4.json "$result/evidence/"
cp /work/inputs/build-image.id "$result/evidence/build-image-provenance.id"
if test -f /work/inputs/build-image-provenance.env; then
	cp /work/inputs/build-image-provenance.env "$result/evidence/"
fi

mkdir "$result/source"
find /work/source-package -mindepth 1 -maxdepth 1 -type f -print0 |
	sort -z | xargs -0 cp -t "$result/source" --
[ "$(find "$result/source" -maxdepth 1 -type f | wc -l)" -eq 5 ] || {
	echo "finalizer did not export the exact source bundle" >&2
	exit 1
}

cat >"$result/evidence/result.env" <<EOF
status=PASS
networked_phase=source-staging-only
offline_builds=1
independent_rebuild=NOT_RUN
source_package=PASS
source_reconstruction=PASS
source_bundle_export=PRESENT
publishable=false
scope=flavor-${flavor}-development
flavor=${flavor}
lto_mode=${lto_mode}
EOF

echo "${flavor} development export PASS; install/coexistence gates remain" >&2
tar --create --file=- --directory="$result" artifacts evidence source

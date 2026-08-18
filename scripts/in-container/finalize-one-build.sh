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
replay_check="$(mktemp /tmp/dkc-simd-replay-XXXXXX.json)"
python3 /work/repo/scripts/in-container/audit-kernel-simd.py \
	/dev/null "$result/artifacts" \
	/work/repo/config/flavors/intentional-simd-symbols.toml \
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
python3 /work/repo/scripts/in-container/attest-one-build.py \
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
python3 - \
	"$result/evidence/attestation.json" \
	"$result/evidence/kbuild-command-audit.json" \
	"$result/evidence/kernel-simd-audit.json" \
	"$result/evidence/attestation-replay/manifest.json" \
	"$result/evidence/post-build-gates.env" \
	/work/inputs/publication-identity.json "$flavor" <<'PY'
import hashlib
import json
import pathlib
import sys

(
    attestation_path,
    kbuild_path,
    simd_path,
    replay_path,
    gates_path,
    identity_path,
    flavor,
) = sys.argv[1:]
identity = json.load(open(identity_path, encoding="utf-8"))
attestation = json.load(open(attestation_path, encoding="utf-8"))
replay = json.load(open(replay_path, encoding="utf-8"))


def record(path: pathlib.Path) -> dict[str, object]:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return {"size": path.stat().st_size, "sha256": digest.hexdigest()}


expected_krel = identity.get("kernel_releases", {}).get(flavor)
expected_lto = identity.get("lto_mode")
if (
    attestation.get("status") != "PASS"
    or attestation.get("flavor") != flavor
    or attestation.get("kernel_release") != expected_krel
    or attestation.get("lto_mode") != expected_lto
):
    raise SystemExit("build attestation identity/status did not pass")
if (
    replay.get("status") != "COMPLETE"
    or replay.get("llvm_major") != attestation.get("llvm_major")
    or replay.get("original_vmlinux", {}).get("sha256")
    != attestation.get("vmlinux_sha256")
    or replay.get("schema_version") != 2
    or replay.get("executable_sections_comparison") != "PASS"
):
    raise SystemExit("attestation replay bundle does not match the accepted vmlinux")
replay_root = pathlib.Path(replay_path).parent
evidence_root = replay_root.parent
for key, path in (
    ("vmlinux_zst", replay_root / "vmlinux.zst"),
    ("system_map", replay_root / "System.map"),
    ("config", replay_root / "config"),
    ("executable_sections", replay_root / "executable-sections.json"),
    ("kbuild_commands", evidence_root / "kbuild-commands.tsv.xz"),
    ("build_tree_inventory", replay_root / "build-tree-inventory.json"),
):
    if replay.get(key) != record(path):
        raise SystemExit(f"attestation replay member differs: {key}")
executable_sections = json.loads(
    (replay_root / "executable-sections.json").read_text(encoding="utf-8")
)
if (
    executable_sections.get("schema_version") != 1
    or executable_sections.get("status") != "PASS"
    or executable_sections.get("llvm_major") != attestation.get("llvm_major")
    or not isinstance(executable_sections.get("sections"), list)
    or not executable_sections["sections"]
):
    raise SystemExit("executable-section replay verification did not pass")
gates = {}
for line in pathlib.Path(gates_path).read_text(encoding="utf-8").splitlines():
    key, separator, value = line.partition("=")
    if not separator or key in gates:
        raise SystemExit("post-build gate result is malformed")
    gates[key] = value
if gates != {
    "package_attestation_rc": "0",
    "kbuild_audit_rc": "0",
    "simd_audit_rc": "0",
    "lintian_rc": "0",
}:
    raise SystemExit(f"one or more post-build gates did not pass: {gates!r}")
for path, label in ((kbuild_path, "Kbuild command"), (simd_path, "kernel SIMD")):
    report = json.load(open(path, encoding="utf-8"))
    if report.get("status") != "PASS":
        raise SystemExit(f"{label} audit did not pass")
    if report.get("lto_mode") != expected_lto:
        raise SystemExit(f"{label} audit has a different LTO mode")
PY

python3 - "$result/evidence/source-package/source-package.json" \
	"$result/evidence/source-package/source-tree.manifest.xz" \
	/work/source-package /work/inputs/publication-identity.json <<'PY'
import hashlib
import json
import lzma
import pathlib
import sys

report_path, manifest_path, bundle_path, identity_path = map(pathlib.Path, sys.argv[1:])
report = json.loads(report_path.read_text(encoding="utf-8"))
identity = json.loads(identity_path.read_text(encoding="utf-8"))
if (
    report.get("status") != "PASS"
    or report.get("reconstruction") != "PASS"
    or report.get("package") != "dkc-linux"
    or report.get("version") != identity.get("package_version")
    or report.get("build_input_digest") != identity.get("build_input_digest")
):
    raise SystemExit("source-package identity or reconstruction did not pass")
files = report.get("files")
if not isinstance(files, list) or len(files) != 5:
    raise SystemExit("source-package report lacks the exact five-file bundle")
actual_names = {path.name for path in bundle_path.iterdir() if path.is_file()}
reported_names = {item.get("name") for item in files if isinstance(item, dict)}
if actual_names != reported_names:
    raise SystemExit("source-package directory and report file sets differ")
for item in files:
    if not isinstance(item, dict):
        raise SystemExit("malformed source-package file record")
    path = bundle_path / str(item.get("name"))
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if digest != item.get("sha256") or path.stat().st_size != item.get("size"):
        raise SystemExit(f"source-package member differs: {path.name}")
manifest = lzma.decompress(manifest_path.read_bytes())
if hashlib.sha256(manifest).hexdigest() != report.get("source_tree_manifest_sha256"):
    raise SystemExit("compressed reconstructed-source manifest differs from its report")
if len(manifest.splitlines()) != report.get("source_tree_entries"):
    raise SystemExit("reconstructed-source manifest entry count differs")
PY

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

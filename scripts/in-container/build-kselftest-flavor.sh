#!/usr/bin/env bash
# Validate accepted kernel/source inputs and compile only the selected selftests.

set -Eeuo pipefail

[ "$#" -eq 6 ] || {
	printf 'usage: build-kselftest-flavor.sh <flavor-result> <output> <flavor> <llvm-major> <profile> <kind>\n' >&2
	exit 2
}
flavor_root="$1"
output="$2"
flavor="$3"
llvm_major="$4"
profile_relative="$5"
kind="$6"

case "$flavor" in
v2 | v3 | v4) ;;
*)
	printf 'invalid kselftest flavor\n' >&2
	exit 2
	;;
esac
[[ "$llvm_major" =~ ^[0-9]+$ ]] || {
	printf 'invalid LLVM major\n' >&2
	exit 2
}
[[ "$profile_relative" =~ ^config/[A-Za-z0-9._-]+$ ]] || {
	printf 'unsafe kselftest profile path\n' >&2
	exit 2
}
[[ "$kind" =~ ^[a-z][a-z0-9-]*$ ]] || {
	printf 'unsafe kselftest result kind\n' >&2
	exit 2
}
profile="/work/repo/$profile_relative"
[ -f "$profile" ] && [ ! -L "$profile" ]
[ -d "$flavor_root/artifacts" ] && [ -d "$flavor_root/evidence" ]
[ -d "$flavor_root/source" ]
[ ! -e "$output/evidence" ]
mkdir "$output/evidence"
evidence="$output/evidence"

grep -qx 'status=PASS' "$flavor_root/evidence/result.env"
grep -qx "flavor=${flavor}" "$flavor_root/evidence/result.env"
(
	cd "$flavor_root/evidence"
	sha256sum --check evidence.sha256 >/dev/null
)
(
	cd "$flavor_root/artifacts"
	sha256sum --check "$flavor_root/evidence/artifacts.sha256" >/dev/null
)

python3 - "$flavor_root" "$flavor" "$llvm_major" "$kind" "$profile" <<'PY'
import hashlib
import json
import pathlib
import re
import shlex
import sys

from dkc.debver import DebianVersion
from dkc.sourcepackage import validate_source_bundle

flavor_root, flavor, llvm_text, kind, profile_path = sys.argv[1:]
flavor_root = pathlib.Path(flavor_root)
profile_path = pathlib.Path(profile_path)
identity = json.loads(
    (flavor_root / "evidence/publication-identity.json").read_text(encoding="utf-8")
)
attestation = json.loads(
    (flavor_root / "evidence/attestation.json").read_text(encoding="utf-8")
)
source_report = json.loads(
    (flavor_root / "evidence/source-package/source-package.json").read_text(
        encoding="utf-8"
    )
)
digest = identity.get("build_input_digest")
kernel_release = identity.get("kernel_releases", {}).get(flavor)
if (
    not isinstance(digest, str)
    or not re.fullmatch(r"[0-9a-f]{64}", digest)
    or source_report.get("build_input_digest") != digest
):
    raise SystemExit("kernel and source evidence have different build identities")
if (
    attestation.get("schema_version") != 2
    or attestation.get("status") != "PASS"
    or attestation.get("flavor") != flavor
    or attestation.get("kernel_release") != kernel_release
    or attestation.get("lto_mode") != identity.get("lto_mode")
    or attestation.get("llvm_major") != int(llvm_text)
):
    raise SystemExit("kernel attestation does not match the requested test build")
package_names = identity.get("package_names", {})
expected_binaries = set(package_names.get("versioned", [])) | set(
    package_names.get("meta", [])
)
bundle = validate_source_bundle(
    flavor_root / "source",
    package="dkc-linux",
    version=str(identity.get("package_version")),
    upstream_version=DebianVersion.parse(
        str(identity.get("debian_source_version"))
    ).upstream_release,
    expected_binary_packages=expected_binaries,
)
if any(source_report.get(key) != value for key, value in bundle.to_dict().items()):
    raise SystemExit("physical source bundle differs from its accepted report")
profile_values = {}
for raw_line in profile_path.read_text(encoding="utf-8").splitlines():
    line = raw_line.strip()
    if not line or line.startswith("#"):
        continue
    key, separator, value = line.partition("=")
    parsed = shlex.split(value) if separator else []
    if len(parsed) != 1 or key in profile_values:
        raise SystemExit("malformed kselftest profile")
    profile_values[key] = parsed[0]
if profile_values.get("DKC_KSELFTEST_PROFILE_KIND") != kind:
    raise SystemExit("kselftest output kind differs from the selected profile")
PY

identity="$flavor_root/evidence/publication-identity.json"
attestation="$flavor_root/evidence/attestation.json"
kernel_release="$(python3 -c \
	'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["kernel_releases"][sys.argv[2]])' \
	"$identity" "$flavor")"
lto_mode="$(python3 -c \
	'import json,sys; value=json.load(open(sys.argv[1], encoding="utf-8"))["lto_mode"]; print(value) if value in ("none", "thin", "full") else sys.exit(1)' \
	"$identity")"
base_package="dkc-linux-base-${kernel_release}"
mapfile -t base_debs < <(
	for deb in "$flavor_root"/artifacts/*.deb; do
		[ "$(dpkg-deb -f "$deb" Package)" = "$base_package" ] && printf '%s\n' "$deb"
	done
)
[ "${#base_debs[@]}" -eq 1 ] || {
	printf 'accepted flavor does not contain exactly one %s package\n' "$base_package" >&2
	exit 1
}
mkdir /work/kernel-package
dpkg-deb --extract "${base_debs[0]}" /work/kernel-package
config="/work/kernel-package/boot/config-${kernel_release}"
[ -f "$config" ]
shipped_config_sha256="$(python3 -c \
	'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["shipped_config_sha256"])' \
	"$attestation")"
[ "$(sha256sum "$config" | awk '{print $1}')" = "$shipped_config_sha256" ] || {
	printf 'shipped kernel configuration differs from the build attestation\n' >&2
	exit 1
}
cp "$config" /work/kernel-package/.config

dsc="$(python3 -c \
	'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["dsc"])' \
	"$flavor_root/evidence/source-package/source-package.json")"
[[ "$dsc" =~ ^[A-Za-z0-9][A-Za-z0-9.+~_-]*[.]dsc$ ]]
dpkg-source --extract "$flavor_root/source/$dsc" /work/source-tree >/dev/null
[ -d /work/source-tree/tools/testing/selftests ]

patch_manifest="$evidence/kselftest-source-patches.sha256"
: >"$patch_manifest"
for source_patch in /work/repo/tests/integration/kselftest-patches/*.patch; do
	[ -f "$source_patch" ] && [ ! -L "$source_patch" ]
	(
		cd /work/repo
		sha256sum "${source_patch#/work/repo/}"
	) >>"$patch_manifest"
	patch --batch --fuzz=0 --directory=/work/source-tree --strip=1 <"$source_patch"
done
[ -s "$patch_manifest" ] || {
	printf 'no kselftest source compatibility patches were applied\n' >&2
	exit 1
}

SOURCE_DATE_EPOCH="$(python3 -c \
	'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["publication_source_date_epoch"])' \
	"$identity")"
export SOURCE_DATE_EPOCH
/work/repo/scripts/in-container/build-kselftest.sh \
	/work/source-tree /work/kernel-package "$evidence" "$llvm_major" \
	"$identity" "$flavor" "$profile"

python3 - "$evidence/kselftest-build.json" "$attestation" \
	"$flavor_root/evidence/source-package/source-package.json" \
	"$patch_manifest" <<'PY'
import hashlib
import json
import pathlib
import sys

report_path, attestation_path, source_report_path, patch_manifest = map(
    pathlib.Path, sys.argv[1:]
)
report = json.loads(report_path.read_text(encoding="utf-8"))
attestation = json.loads(attestation_path.read_text(encoding="utf-8"))
source_report = json.loads(source_report_path.read_text(encoding="utf-8"))
if report.get("kernel_config_sha256") != attestation.get("shipped_config_sha256"):
    raise SystemExit("selftest bundle was not built against the shipped configuration")
report["source_package_report_sha256"] = hashlib.sha256(
    source_report_path.read_bytes()
).hexdigest()
report["source_tree_manifest_sha256"] = source_report.get(
    "source_tree_manifest_sha256"
)
report["source_patch_manifest_sha256"] = hashlib.sha256(
    patch_manifest.read_bytes()
).hexdigest()
report["source_patch_count"] = len(patch_manifest.read_text(encoding="utf-8").splitlines())
report_path.write_text(
    json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
)
PY

cp "$identity" "$evidence/publication-identity.json"
cp "$attestation" "$evidence/kernel-attestation.json"
cp "$flavor_root/evidence/source-package/source-package.json" \
	"$evidence/source-package.json"
cp "$profile" "$evidence/profile.env"
cat >"$evidence/result.env" <<EOF
status=PASS
flavor=${flavor}
profile_kind=${kind}
kernel_release=${kernel_release}
lto_mode=${lto_mode}
publishable=false
EOF
printf 'kselftest-only build PASS: flavor=%s profile=%s kernel=%s\n' \
	"$flavor" "$kind" "$kernel_release" >&2

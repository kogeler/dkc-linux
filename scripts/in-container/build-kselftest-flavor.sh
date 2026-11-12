#!/usr/bin/env bash
# Validate accepted kernel/source inputs and compile only the selected selftests.

set -Eeuo pipefail

[ "$#" -eq 5 ] || {
	printf 'usage: build-kselftest-flavor.sh <flavor-result> <output> <flavor> <llvm-major> <kind>\n' >&2
	exit 2
}
flavor_root="$1"
output="$2"
flavor="$3"
llvm_major="$4"
kind="$5"

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
[[ "$kind" =~ ^[a-z][a-z0-9-]*$ ]] || {
	printf 'unsafe kselftest result kind\n' >&2
	exit 2
}
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

# The selftest selection and its source patches belong to the source profile of
# the accepted kernel, never to whichever series the repository targets next.
source_version="$(python3 -c \
	'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["debian_source_version"])' \
	"$flavor_root/evidence/publication-identity.json")"
profile=/work/kselftest-profile.env
PYTHONPATH=/work/repo python3 -m dkc.sourceprofile \
	/work/repo "$source_version" kselftest-environment >"$profile"
kselftest_patch_dir="$(PYTHONPATH=/work/repo python3 -m dkc.sourceprofile \
	/work/repo "$source_version" kselftest-patch-directory)"

PYTHONPATH=/work/repo python3 -m dkc.cli.build_kselftest_flavor inputs "$flavor_root" "$flavor" "$llvm_major" "$kind" "$profile"

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
patch_results="$evidence/kselftest-source-patches.results"
: >"$patch_manifest"
: >"$patch_results"
# A profile may legitimately need no patch when its upstream source already
# carries every fix; the manifest then records that exactly nothing was applied.
source_patches=()
if [ -d "$kselftest_patch_dir" ]; then
	mapfile -t source_patches < <(find "$kselftest_patch_dir" -mindepth 1 -maxdepth 1 -print | sort)
fi
for source_patch in "${source_patches[@]}"; do
	if [ ! -f "$source_patch" ] || [ -L "$source_patch" ] || [[ "$source_patch" != *.patch ]]; then
		printf 'unexpected kselftest source patch entry: %s\n' "$source_patch" >&2
		exit 1
	fi
	(
		cd /work/repo
		sha256sum "${source_patch#/work/repo/}"
	) >>"$patch_manifest"
	# The changed lines must match exactly; context that moved since the pinned
	# source is tolerated, and a fix the source already carries is recorded as
	# already present instead of being reverted or applied twice.
	patch_result="$(PYTHONPATH=/work/repo python3 -m dkc.sourcepatch \
		/work/source-tree "$source_patch")"
	printf '%s\t%s\n' "${source_patch#/work/repo/}" "$patch_result" >>"$patch_results"
	printf 'kselftest source patch %s: %s\n' "${source_patch##*/}" "$patch_result" >&2
done
for manifest in "$patch_manifest" "$patch_results"; do
	[ "$(wc -l <"$manifest")" -eq "${#source_patches[@]}" ] || {
		printf 'kselftest source patch manifest differs from the source profile\n' >&2
		exit 1
	}
done

SOURCE_DATE_EPOCH="$(python3 -c \
	'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["publication_source_date_epoch"])' \
	"$identity")"
export SOURCE_DATE_EPOCH
/work/repo/scripts/in-container/build-kselftest.sh \
	/work/source-tree /work/kernel-package "$evidence" "$llvm_major" \
	"$identity" "$flavor" "$profile"

PYTHONPATH=/work/repo python3 -m dkc.cli.build_kselftest_flavor bind-report "$evidence/kselftest-build.json" "$attestation" \
	"$flavor_root/evidence/source-package/source-package.json" \
	"$patch_manifest" "$patch_results"

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

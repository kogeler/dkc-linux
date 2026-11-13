#!/usr/bin/env bash
# Build and reconstruct the exact source package before compiling any flavor.

set -Eeuo pipefail

if [ "$#" -ne 6 ]; then
	printf 'usage: build-source-package.sh <prepared-source> <repo> <inputs> <bundle> <reconstructed-source> <evidence>\n' >&2
	exit 2
fi
prepared="$(realpath "$1")"
repo="$(realpath "$2")"
inputs="$(realpath "$3")"
bundle="$4"
reconstructed="$5"
evidence="$6"
test -d "$prepared" -a -d "$repo" -a -d "$inputs"
test ! -e "$bundle" -a ! -e "$reconstructed"
mkdir -p "$bundle" "$evidence"

identity_output="$(
	PYTHONPATH="$repo" python3 -m dkc.cli.build_source_package \
		"$inputs/publication-identity.json" "$inputs/source-inventory.json"
)"
readarray -t identity_fields <<<"$identity_output"
[ "${#identity_fields[@]}" -eq 4 ]
package_version="${identity_fields[0]}"
publication_epoch="${identity_fields[1]}"
upstream_version="${identity_fields[2]}"
orig_name="${identity_fields[3]}"
[[ "$package_version" =~ ^[0-9A-Za-z.+:~_-]+$ ]]
[[ "$publication_epoch" =~ ^[1-9][0-9]*$ ]]
[[ "$upstream_version" =~ ^[0-9A-Za-z.+~_-]+$ ]]
[[ "$orig_name" =~ ^[A-Za-z0-9][A-Za-z0-9._+~-]*\.orig\.tar\.xz$ ]]

PYTHONPATH="$repo" python3 -m dkc.cli.prepare_source_tree "$prepared" "$repo" "$inputs"

# The authenticated Debian source carries its previously generated control
# file. Identity injection changes the changelog source name, so regenerate
# control before dpkg-source reads both and correctly rejects a mixed identity.
DKC_BUILD_PROFILES=
# shellcheck disable=SC1091
. "$prepared/debian/dkc/build-profiles"
: "${DKC_BUILD_PROFILES:?missing DKC build profiles}"
export DEB_BUILD_PROFILES="$DKC_BUILD_PROFILES"
export DEB_BUILD_OPTIONS=noautodbgsym
export SOURCE_DATE_EPOCH="$publication_epoch"
(
	cd "$prepared"
	make -f debian/rules debian/control-real >/dev/null
	dpkg-checkbuilddeps debian/control
)

parent="$(dirname "$prepared")"
source_name="$(basename "$prepared")"
orig_input="$inputs/$orig_name"
orig_for_source="$parent/dkc-linux_${upstream_version}.orig.tar.xz"
test -f "$orig_input" -a ! -e "$orig_for_source"
ln "$orig_input" "$orig_for_source"

(
	cd "$parent"
	EDITOR="$repo/dkc/cli/normalize_quilt_patch.py" \
		VISUAL="$repo/dkc/cli/normalize_quilt_patch.py" \
		dpkg-source --commit "$source_name" \
		dkc-x86-64-baselines.patch </dev/null >/dev/null
)
touch --date="@${publication_epoch}" \
	"$prepared/debian/patches/dkc-x86-64-baselines.patch" \
	"$prepared/debian/patches/series"

(
	cd "$prepared"
	make -f debian/rules debian/control-real >/dev/null
	PYTHONPATH="$repo" python3 -m dkc.cli.prepare_source_tree \
		--normalize-public-metadata "$prepared" "$publication_epoch"
	dpkg-checkbuilddeps debian/control
	dpkg-buildpackage --build=source --no-sign -sa
	# Compare clean source trees, not dpkg-buildpackage's transient bookkeeping.
	debian/rules clean >/dev/null
)
PYTHONPATH="$repo" python3 -m dkc.cli.prepare_source_tree \
	--normalize-public-modes "$prepared"

version_filename="${package_version#*:}"
names=(
	"dkc-linux_${version_filename}.dsc"
	"dkc-linux_${upstream_version}.orig.tar.xz"
	"dkc-linux_${version_filename}.debian.tar.xz"
	"dkc-linux_${version_filename}_source.changes"
	"dkc-linux_${version_filename}_source.buildinfo"
)
for name in "${names[@]}"; do
	test -f "$parent/$name"
	cp --reflink=auto --preserve=mode,timestamps "$parent/$name" "$bundle/$name"
done

# dpkg-source deliberately applies the extractor's umask to newly introduced
# files.  The surrounding container uses 077 for private scratch data, while
# this tree is the public source deliverable and its prepared modes are
# explicitly 0644/0755.  Pin the public extraction contract independently of
# the caller's security umask so reconstruction is deterministic.
(
	umask 022
	dpkg-source -x "$bundle/dkc-linux_${version_filename}.dsc" "$reconstructed" >/dev/null
)
PYTHONPATH="$repo" python3 -m dkc.cli.audit_source_package \
	"$repo" "$bundle" "$inputs/publication-identity.json" \
	"$prepared" "$reconstructed" "$evidence"
(
	cd "$evidence"
	xz --threads=1 --check=sha256 -1 source-tree.manifest
	sha256sum source-tree.manifest.xz >source-tree.manifest.xz.sha256
)

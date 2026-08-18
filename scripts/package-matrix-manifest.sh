#!/usr/bin/env bash
# Write or verify the exact checksum scopes of a package-matrix result.

set -Eeuo pipefail

if [ "$#" -ne 2 ]; then
	printf 'usage: package-matrix-manifest.sh <write|verify-lifecycle|verify-full> <matrix-result>\n' >&2
	exit 2
fi
mode="$1"
root="$(realpath "$2")"

case "$mode" in
write | verify-lifecycle | verify-full) ;;
*)
	printf 'invalid package-matrix manifest mode: %s\n' "$mode" >&2
	exit 2
	;;
esac

test -d "$root/evidence"
test ! -L "$root/evidence"
scopes=(evidence)
for scope in client-image client-headers; do
	test ! -L "$root/$scope"
	if [ -e "$root/$scope" ]; then
		test -d "$root/$scope"
		scopes+=("$scope")
	fi
done

require_regular_tree() {
	local unexpected
	unexpected="$(
		cd "$root"
		find "$@" ! -type d ! -type f -print -quit
	)"
	test -z "$unexpected"
}

write_lifecycle_manifest() {
	require_regular_tree "${scopes[@]}"
	(
		cd "$root"
		find "${scopes[@]}" -type f ! -path 'evidence/evidence.sha256' -print0 |
			sort -z | xargs -0 -r sha256sum
	) >"$root/evidence/evidence.sha256"
}

verify_lifecycle_manifest() {
	test -s "$root/evidence/evidence.sha256"
	require_regular_tree "${scopes[@]}"
	# Recreating the sorted manifest, rather than merely checking its listed
	# hashes, proves both file contents and the exact exported path set.
	(
		cd "$root"
		find "${scopes[@]}" -type f ! -path 'evidence/evidence.sha256' -print0 |
			sort -z | xargs -0 -r sha256sum
	) | cmp - "$root/evidence/evidence.sha256"
}

write_flat_manifest() {
	test ! -L "$root/flat-repository"
	require_regular_tree flat-repository
	(
		cd "$root"
		find flat-repository -type f -print0 |
			sort -z | xargs -0 -r sha256sum
	) >"$root/evidence/flat-repository.sha256"
}

verify_flat_manifest() {
	test -d "$root/flat-repository"
	test ! -L "$root/flat-repository"
	test -s "$root/evidence/flat-repository.sha256"
	require_regular_tree flat-repository
	(
		cd "$root"
		find flat-repository -type f -print0 |
			sort -z | xargs -0 -r sha256sum
	) | cmp - "$root/evidence/flat-repository.sha256"
}

case "$mode" in
write)
	[ ! -d "$root/flat-repository" ] || write_flat_manifest
	write_lifecycle_manifest
	;;
verify-lifecycle)
	verify_lifecycle_manifest
	;;
verify-full)
	verify_lifecycle_manifest
	verify_flat_manifest
	unexpected="$(
		find "$root" -mindepth 1 -maxdepth 1 \
			! -name evidence ! -name client-image ! -name client-headers \
			! -name flat-repository -print -quit
	)"
	test -z "$unexpected"
	;;
esac

printf 'package-matrix manifest %s PASS\n' "$mode"

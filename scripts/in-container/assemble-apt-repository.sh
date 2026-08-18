#!/usr/bin/env bash
# Assemble the complete unsigned repository and a strict signing request.

set -Eeuo pipefail

if [ "$#" -ne 11 ]; then
	printf 'usage: assemble-apt-repository.sh <matrix-result> <output> <public-keyring> <primary-fingerprint> <signing-subkeys> <epoch> <generation> <previous-pool-result-or-empty> <previous-state-result-or-empty> <retention-mode> <retention-max-bytes-or-empty>\n' >&2
	exit 2
fi
matrix=""
if [ -n "$1" ]; then
	matrix="$(realpath "$1")"
fi
output="$2"
public_keyring="$(realpath "$3")"
primary_fingerprint="$(realpath "$4")"
signing_subkeys="$(realpath "$5")"
epoch="$6"
generation="$7"
previous_pool="$8"
previous_state="$9"
retention_mode="${10}"
retention_max_bytes="${11}"

[[ "$epoch" =~ ^[1-9][0-9]*$ ]]
[[ "$generation" =~ ^[0-9]+$ ]]
case "$retention_mode" in
series) test -z "$retention_max_bytes" ;;
series-size) [[ "$retention_max_bytes" =~ ^[1-9][0-9]*$ ]] ;;
*) exit 2 ;;
esac
test -s "$public_keyring" -a -s "$primary_fingerprint" -a -s "$signing_subkeys"
test -d "$output" -a ! -L "$output"
test ! -e "$output/repository" -a ! -e "$output/handoff"
assembly_args=()
if [ -n "$matrix" ]; then
	test -d "$matrix/flat-repository"
	test -d "$matrix/evidence"
	grep -qx 'status=PASS' "$matrix/evidence/result.env"
	grep -qx 'package_matrix=PASS' "$matrix/evidence/result.env"
	scripts/package-matrix-manifest.sh verify-full "$matrix" >/dev/null
	assembly_args=(
		--flat "$matrix/flat-repository"
		--identity "$matrix/evidence/publication-identity.json"
	)
else
	test -n "$previous_pool" -a -n "$previous_state"
	assembly_args=(--maintenance)
fi

mkdir -p "$output/evidence" "$output/handoff"
active_subkey="$(tail -n 1 "$signing_subkeys")"
[[ "$active_subkey" =~ ^[0-9A-F]{40}$ ]]
keyring_epoch="$({
	gpg --batch --show-keys --with-colons "$public_keyring" |
		awk -F: -v active="$active_subkey" '
			$1 == "sub" {created = $6; pending = 1; next}
			pending && $1 == "fpr" {
				if ($10 == active) {print created; found++}
				pending = 0
			}
			END {if (found != 1) exit 1}
		'
} 2>/dev/null)"
[[ "$keyring_epoch" =~ ^[1-9][0-9]*$ ]]
key_digest="$(sha256sum "$public_keyring" | cut -c1-16)"
keyring_version="1.0+$(date --utc --date="@${keyring_epoch}" +%Y%m%d).${key_digest}"
scripts/in-container/build-archive-keyring.sh \
	"$public_keyring" "$primary_fingerprint" "$signing_subkeys" \
	"$keyring_version" "$keyring_epoch" /work/keyring-work /work/keyring-bundle

# .buildinfo and .changes describe this particular build invocation and are
# deliberately not stable archive objects. Retain them as run evidence, while
# the APT pool receives only the reproducible binary and source inputs.
mkdir -p "$output/evidence/source-upload-metadata"
keyring_upload_metadata=(
	/work/keyring-bundle/dkc-archive-keyring_*_source.buildinfo
	/work/keyring-bundle/dkc-archive-keyring_*_source.changes
)
upload_metadata=("${keyring_upload_metadata[@]}")
expected_metadata=2
if [ -n "$matrix" ]; then
	linux_upload_metadata=(
		"$matrix"/flat-repository/dkc-linux_*_source.buildinfo
		"$matrix"/flat-repository/dkc-linux_*_source.changes
	)
	upload_metadata+=("${linux_upload_metadata[@]}")
	expected_metadata=4
fi
for path in "${upload_metadata[@]}"; do
	test -f "$path" -a ! -L "$path"
	install -m 0644 "$path" "$output/evidence/source-upload-metadata/"
done
test "$(find "$output/evidence/source-upload-metadata" -maxdepth 1 -type f | wc -l)" -eq "$expected_metadata"

previous_args=()
if [ -n "$previous_pool" ] || [ -n "$previous_state" ]; then
	test -d "$previous_pool" -a ! -L "$previous_pool"
	test -d "$previous_state" -a ! -L "$previous_state"
	previous_args=(
		--previous-pool-result "$previous_pool"
		--previous-state-result "$previous_state"
	)
fi

retention_args=(--retention-mode "$retention_mode")
if [ -n "$retention_max_bytes" ]; then
	retention_args+=(--retention-max-bytes "$retention_max_bytes")
fi
python3 scripts/in-container/build-signed-repository.py assemble \
	"${assembly_args[@]}" \
	--keyring-bundle /work/keyring-bundle \
	--public-keyring "$public_keyring" \
	--primary-fingerprint "$primary_fingerprint" \
	--signing-subkeys "$signing_subkeys" \
	--output "$output/repository" \
	--request "$output/handoff/signing-request.json" \
	--epoch "$epoch" \
	--generation "$generation" \
	"${retention_args[@]}" \
	"${previous_args[@]}" \
	>"$output/evidence/assembly.json"

cat >"$output/evidence/result.env" <<EOF
status=PASS
repository_assembly=PASS
signed=false
generation=${generation}
publishable=false
EOF
printf 'unsigned repository assembly PASS\n' >&2

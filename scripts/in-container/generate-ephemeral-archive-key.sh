#!/usr/bin/env bash
# Generate a disposable primary plus overlapping signing subkeys for local tests.

set -Eeuo pipefail

[ "$#" -eq 1 ] || {
	printf 'usage: generate-ephemeral-archive-key.sh <output>\n' >&2
	exit 2
}
output="$1"
test -d "$output" -a ! -L "$output"
test -z "$(find "$output" -mindepth 1 -maxdepth 1 -print -quit)"
chmod 0700 "$output"
work="$(mktemp -d /work/ephemeral-archive-key-XXXXXX)"
trap 'rm -rf -- "$work"' EXIT
gpg_home="$work/gnupg"
mkdir -m 0700 "$gpg_home"
passphrase="$output/passphrase"
head -c 48 /dev/urandom | base64 -w0 >"$passphrase"
chmod 0600 "$passphrase"

gpg --homedir "$gpg_home" --batch --pinentry-mode loopback \
	--passphrase-file "$passphrase" --quick-generate-key \
	'DKC ephemeral archive <ephemeral@dkc.invalid>' ed25519 cert 90d >/dev/null
primary="$(gpg --homedir "$gpg_home" --batch --with-colons --list-secret-keys |
	awk -F: '$1 == "fpr" {print $10; exit}')"
[[ "$primary" =~ ^[0-9A-F]{40}$ ]]
for _ in 1 2; do
	gpg --homedir "$gpg_home" --batch --pinentry-mode loopback \
		--passphrase-file "$passphrase" --quick-add-key \
		"$primary" ed25519 sign 90d >/dev/null
done
mapfile -t subkeys < <(
	gpg --homedir "$gpg_home" --batch --with-colons --list-secret-keys |
		awk -F: '$1 == "ssb" {want = 1; next} want && $1 == "fpr" {print $10; want = 0}'
)
[ "${#subkeys[@]}" -eq 2 ]
active="${subkeys[1]}"
printf '%s\n' "$primary" >"$output/archive-primary.fingerprint"
printf '%s\n' "${subkeys[@]}" >"$output/archive-signing-subkeys.fingerprints"
gpg --homedir "$gpg_home" --batch --export "$primary" \
	>"$output/dkc-archive-keyring.gpg"
gpg --homedir "$gpg_home" --batch --pinentry-mode loopback \
	--passphrase-file "$passphrase" --export-secret-subkeys "${active}!" \
	>"$output/signing-subkey.gpg"
test -s "$output/dkc-archive-keyring.gpg" -a -s "$output/signing-subkey.gpg"
chmod 0600 "$output/signing-subkey.gpg"
printf 'ephemeral archive key generation PASS\n' >&2

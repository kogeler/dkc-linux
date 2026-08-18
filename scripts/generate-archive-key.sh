#!/usr/bin/env bash
# Provision the four-year archive certificate on an offline trusted machine.

# shellcheck source=scripts/lib/common.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"

dkc::refuse_root
dkc::require_cmd gpg gpgv base64 sha256sum date awk install realpath
umask 077

[ "$#" -eq 1 ] || dkc::die "usage: generate-archive-key.sh <new-offline-workspace>"
workspace_input="$1"
[ ! -e "$workspace_input" ] || dkc::die "offline key workspace already exists"
workspace="$(realpath -m "$workspace_input")"
case "$workspace" in
"$DKC_ROOT" | "$DKC_ROOT"/*) dkc::die "private archive workspace must be outside the repository" ;;
esac
mkdir -p "$workspace"
for public_name in \
	dkc-archive-keyring.gpg \
	archive-primary.fingerprint \
	archive-signing-subkeys.fingerprints; do
	[ ! -e "$DKC_ROOT/keys/$public_name" ] ||
		dkc::die "tracked archive certificate already exists; rotate through the documented procedure"
done
if [ ! -d "$DKC_ROOT/keys" ] || [ -L "$DKC_ROOT/keys" ]; then
	dkc::die "tracked keys/ must be a plain directory"
fi
mkdir -m 0700 "$workspace/gnupg" "$workspace/backup" "$workspace/github" \
	"$workspace/public" "$workspace/revocation" "$workspace/evidence"
passphrase_file="$workspace/.passphrase.tmp"
raw_subkey="$workspace/.online-signing-subkey.gpg"
restore_home="$workspace/.restore-test"
online_home="$workspace/.online-test"
revocation_home="$workspace/.revocation-test"
restore_challenge="$workspace/.restore-challenge"
restore_signature="$workspace/.restore-signature"
online_signature="$workspace/.online-signature"
cleanup_sensitive_scratch() {
	rm -f -- "$passphrase_file" "$raw_subkey" "$restore_challenge" \
		"$restore_signature" "$online_signature"
	rm -rf -- "$restore_home" "$online_home" "$revocation_home"
}
trap cleanup_sensitive_scratch EXIT

printf 'Enter a new archive signing passphrase: ' >&2
IFS= read -r -s passphrase
printf '\nRepeat the archive signing passphrase: ' >&2
IFS= read -r -s confirmation
printf '\n' >&2
[ -n "$passphrase" ] || dkc::die "archive signing passphrase must not be empty"
[ "$passphrase" = "$confirmation" ] || dkc::die "archive signing passphrases differ"
case "$passphrase" in
*$'\n'* | *$'\r'*) dkc::die "archive signing passphrase must be one line" ;;
esac
printf '%s' "$passphrase" >"$passphrase_file"
unset passphrase confirmation

gpg_home="$workspace/gnupg"
identity='DKC Archive Signing Certificate <archive-signing@dkc.invalid>'
gpg --homedir "$gpg_home" --batch --pinentry-mode loopback \
	--passphrase-file "$passphrase_file" --quick-generate-key \
	"$identity" ed25519 cert 4y >/dev/null
primary="$(gpg --homedir "$gpg_home" --batch --with-colons --list-secret-keys |
	awk -F: '$1 == "fpr" {print $10; exit}')"
[[ "$primary" =~ ^[0-9A-F]{40}$ ]] || dkc::die "could not determine the archive primary fingerprint"
gpg --homedir "$gpg_home" --batch --pinentry-mode loopback \
	--passphrase-file "$passphrase_file" --quick-add-key \
	"$primary" ed25519 sign 4y >/dev/null
subkey="$(gpg --homedir "$gpg_home" --batch --with-colons --list-secret-keys |
	awk -F: '$1 == "ssb" {want = 1; next} want && $1 == "fpr" {print $10; exit}')"
[[ "$subkey" =~ ^[0-9A-F]{40}$ ]] || dkc::die "could not determine the archive signing-subkey fingerprint"

mapfile -t key_times < <(
	gpg --homedir "$gpg_home" --batch --with-colons --list-keys "$primary" |
		awk -F: '$1 == "pub" || $1 == "sub" {print $1, $6, $7}'
)
[ "${#key_times[@]}" -eq 2 ] || dkc::die "archive certificate has an unexpected key graph"
four_year_seconds=$((4 * 365 * 86400))
for record in "${key_times[@]}"; do
	read -r kind created expires <<<"$record"
	[[ "$created" =~ ^[1-9][0-9]*$ && "$expires" =~ ^[1-9][0-9]*$ ]] ||
		dkc::die "archive ${kind} does not have finite timestamps"
	[ $((expires - created)) -eq "$four_year_seconds" ] ||
		dkc::die "archive ${kind} validity is not exactly four GnuPG years"
done

gpg --homedir "$gpg_home" --batch --export "$primary" \
	>"$workspace/public/dkc-archive-keyring.gpg"
printf '%s\n' "$primary" >"$workspace/public/archive-primary.fingerprint"
printf '%s\n' "$subkey" >"$workspace/public/archive-signing-subkeys.fingerprints"
revocation_source="$gpg_home/openpgp-revocs.d/${primary}.rev"
[ -s "$revocation_source" ] || dkc::die "GnuPG did not create the primary revocation certificate"
grep -q 'BEGIN PGP PUBLIC KEY BLOCK' "$revocation_source" ||
	dkc::die "generated revocation certificate is malformed"
install -m 0600 "$revocation_source" "$workspace/revocation/${primary}.rev"

gpg --homedir "$gpg_home" --batch --pinentry-mode loopback \
	--passphrase-file "$passphrase_file" --armor --export-secret-keys "$primary" \
	>"$workspace/backup/dkc-archive-primary-secret.asc"
gpg --homedir "$gpg_home" --batch --pinentry-mode loopback \
	--passphrase-file "$passphrase_file" --export-secret-subkeys "${subkey}!" \
	>"$raw_subkey"
base64 -w0 "$raw_subkey" >"$workspace/github/APT_GPG_SIGNING_SUBKEY_B64"
cp "$passphrase_file" "$workspace/github/APT_GPG_PASSPHRASE"
chmod 0600 "$workspace/backup/dkc-archive-primary-secret.asc" \
	"$workspace/github/APT_GPG_SIGNING_SUBKEY_B64" \
	"$workspace/github/APT_GPG_PASSPHRASE"
[ "$(wc -c <"$workspace/github/APT_GPG_SIGNING_SUBKEY_B64")" -le 49152 ] ||
	dkc::die "encoded online signing subkey exceeds GitHub's 48 KiB limit"

mkdir -m 0700 "$restore_home" "$online_home" "$revocation_home"
printf 'DKC archive recovery check\n' >"$restore_challenge"
gpg --homedir "$restore_home" --batch --import \
	"$workspace/backup/dkc-archive-primary-secret.asc" >/dev/null 2>&1
restore_available="$(gpg --homedir "$restore_home" --batch --with-colons --list-secret-keys |
	awk -F: '$1 == "sec" || $1 == "ssb" {if ($15 == "+") count++} END {print count + 0}')"
[ "$restore_available" -eq 2 ] || dkc::die "offline primary-secret restoration test failed"
gpg --homedir "$restore_home" --batch --pinentry-mode loopback \
	--passphrase-file "$passphrase_file" --local-user "${subkey}!" \
	--detach-sign --output "$restore_signature" "$restore_challenge"
gpgv --keyring "$workspace/public/dkc-archive-keyring.gpg" \
	"$restore_signature" "$restore_challenge" >/dev/null 2>&1
gpg --homedir "$online_home" --batch --import "$raw_subkey" >/dev/null 2>&1
online_primary_marker="$(gpg --homedir "$online_home" --batch --with-colons --list-secret-keys |
	awk -F: '$1 == "sec" {print $15; exit}')"
online_available="$(gpg --homedir "$online_home" --batch --with-colons --list-secret-keys |
	awk -F: '$1 == "ssb" && $15 == "+" {count++} END {print count + 0}')"
if [ "$online_primary_marker" != '#' ] || [ "$online_available" -ne 1 ]; then
	dkc::die "online signing-subkey isolation test failed"
fi
gpg --homedir "$online_home" --batch --pinentry-mode loopback \
	--passphrase-file "$passphrase_file" --local-user "${subkey}!" \
	--detach-sign --output "$online_signature" "$restore_challenge"
gpgv --keyring "$workspace/public/dkc-archive-keyring.gpg" \
	"$online_signature" "$restore_challenge" >/dev/null 2>&1
gpg --homedir "$revocation_home" --batch --import \
	"$workspace/public/dkc-archive-keyring.gpg" >/dev/null 2>&1
sed 's/^://' "$workspace/revocation/${primary}.rev" |
	gpg --homedir "$revocation_home" --batch --import >/dev/null 2>&1
revocation_status="$(gpg --homedir "$revocation_home" --batch --with-colons --list-keys "$primary" |
	awk -F: '$1 == "pub" {print $2; exit}')"
[ "$revocation_status" = r ] || dkc::die "primary revocation certificate verification failed"

primary_created="$(awk '$1 == "pub" {print $2}' <<<"${key_times[0]}")"
primary_expires="$(awk '$1 == "pub" {print $3}' <<<"${key_times[0]}")"
subkey_created="$(awk '$1 == "sub" {print $2}' <<<"${key_times[1]}")"
subkey_expires="$(awk '$1 == "sub" {print $3}' <<<"${key_times[1]}")"
cat >"$workspace/evidence/key-lifecycle.env" <<EOF
status=PASS
primary_fingerprint=${primary}
signing_subkey_fingerprint=${subkey}
primary_created_utc=$(date --utc --date="@${primary_created}" +%Y-%m-%dT%H:%M:%SZ)
primary_expires_utc=$(date --utc --date="@${primary_expires}" +%Y-%m-%dT%H:%M:%SZ)
signing_subkey_created_utc=$(date --utc --date="@${subkey_created}" +%Y-%m-%dT%H:%M:%SZ)
signing_subkey_expires_utc=$(date --utc --date="@${subkey_expires}" +%Y-%m-%dT%H:%M:%SZ)
validity_seconds=${four_year_seconds}
revocation_certificate=PASS
offline_restore=PASS
online_primary_secret=UNAVAILABLE
online_secret_subkeys=1
EOF
(
	cd "$workspace"
	find public backup revocation evidence -type f \
		! -path 'evidence/offline-material.sha256' -print0 |
		sort -z | xargs -0 sha256sum
) >"$workspace/evidence/offline-material.sha256"

cleanup_sensitive_scratch
install -m 0644 "$workspace/public/dkc-archive-keyring.gpg" \
	"$DKC_ROOT/keys/dkc-archive-keyring.gpg"
install -m 0644 "$workspace/public/archive-primary.fingerprint" \
	"$DKC_ROOT/keys/archive-primary.fingerprint"
install -m 0644 "$workspace/public/archive-signing-subkeys.fingerprints" \
	"$DKC_ROOT/keys/archive-signing-subkeys.fingerprints"

dkc::ok "archive certificate provisioned; public files installed under keys/"
dkc::log "offline workspace: $workspace"
dkc::log "transfer only public/ and the two github/ handoff files to a networked administrator"

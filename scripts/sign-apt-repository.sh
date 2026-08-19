#!/usr/bin/env bash
# Sign one strict unsigned-repository handoff with the protected online subkey.

# shellcheck source=scripts/lib/common.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"

dkc::refuse_root
dkc::require_cmd podman tar realpath base64
dkc::install_cleanup_trap
umask 077

[ "$#" -eq 6 ] || dkc::die \
	"usage: sign-apt-repository.sh <toolbox> <unsigned-result> <keys-dir> <clock-skew-seconds> <safety-seconds> <lifecycle-decision-or-empty>"
toolbox="$1"
unsigned="$(realpath "$2")"
keys_dir="$(realpath "$3")"
clock_skew_seconds="$4"
safety_seconds="$5"
lifecycle_decision="$6"
[[ "$clock_skew_seconds" =~ ^[0-9]+$ ]] || dkc::die "invalid signing clock-skew allowance"
[[ "$safety_seconds" =~ ^[0-9]+$ ]] || dkc::die "invalid signing safety interval"
case "${DKC_APT_EPHEMERAL_SIGNING:-0}" in
0)
	signing_mode=production
	[ "$keys_dir" = "$DKC_ROOT/keys" ] ||
		dkc::die "production signing requires the tracked keys/ directory"
	;;
1)
	signing_mode=ephemeral
	[ -z "${GITHUB_ACTIONS:-}" ] ||
		[ "${DKC_APT_PULL_REQUEST_QUALIFICATION:-0}" = 1 ] ||
		dkc::die "ephemeral signing is forbidden in GitHub Actions"
	;;
*) dkc::die "DKC_APT_EPHEMERAL_SIGNING must be exactly 0 or 1" ;;
esac
stage="$DKC_RUN_DIR/apt-signature"
secret_dir="$stage/secrets"
mkdir -p "$secret_dir" "$stage/output/evidence"
chmod 0700 "$secret_dir"
dkc::register_resource path "$stage"
output_root="$DKC_ROOT/out/apt-signature"
output="$output_root/$DKC_RUN_ID"
failure_phase=input

export_signing_failure() {
	local rc="$1"
	set +e
	trap - ERR
	cat >"$stage/output/evidence/result.env" <<EOF
status=FAIL
repository_signing=FAIL
failure_phase=${failure_phase}
publishable=false
exit_code=${rc}
EOF
	(
		cd "$stage/output"
		find . -type f ! -path './evidence/evidence.sha256' -print0 |
			sort -z | xargs -0 -r sha256sum
	) >"$stage/output/evidence/evidence.sha256"
	mkdir -p "$output_root"
	if [ -e "$output" ] || ! mv "$stage/output" "$output"; then
		dkc::warn "could not retain APT signing failure evidence: $output"
		exit "$rc"
	fi
	ln -sfn "$DKC_RUN_ID" "$output_root/latest-failed"
	dkc::warn "retained APT signing failure evidence: $output"
	exit "$rc"
}

signing_on_error() {
	local rc=$?
	export_signing_failure "$rc"
}
trap signing_on_error ERR

[ -n "${APT_GPG_SIGNING_SUBKEY_B64:-}" ] || {
	dkc::warn "missing production-signing secret APT_GPG_SIGNING_SUBKEY_B64"
	false
}
[ -n "${APT_GPG_PASSPHRASE:-}" ] || {
	dkc::warn "missing production-signing secret APT_GPG_PASSPHRASE"
	false
}
[ "${#APT_GPG_SIGNING_SUBKEY_B64}" -le 49152 ] || {
	dkc::warn "encoded signing subkey exceeds GitHub's 48 KiB secret limit"
	false
}
case "$APT_GPG_PASSPHRASE" in
*$'\n'* | *$'\r'*)
	dkc::warn "APT_GPG_PASSPHRASE must be one non-empty line"
	false
	;;
esac
if ! printf '%s' "$APT_GPG_SIGNING_SUBKEY_B64" | base64 --decode >"$secret_dir/subkey.gpg"; then
	dkc::warn "APT_GPG_SIGNING_SUBKEY_B64 is not valid base64"
	false
fi
[ -s "$secret_dir/subkey.gpg" ] || {
	dkc::warn "decoded signing subkey is empty"
	false
}
printf '%s' "$APT_GPG_PASSPHRASE" >"$secret_dir/passphrase"
unset APT_GPG_SIGNING_SUBKEY_B64 APT_GPG_PASSPHRASE

if [ ! -d "$unsigned/repository" ] || [ ! -f "$unsigned/handoff/signing-request.json" ]; then
	dkc::warn "unsigned repository handoff is incomplete: $unsigned"
	false
fi
(
	cd "$unsigned"
	sha256sum --check evidence/evidence.sha256 >/dev/null
)
grep -qx 'status=PASS' "$unsigned/evidence/result.env"
podman image exists "$toolbox" || {
	dkc::warn "toolbox image is missing; run: make image"
	false
}
[ "$(podman info --format '{{.Host.Security.Rootless}}' 2>/dev/null)" = true ] || {
	dkc::warn "rootless podman is required"
	false
}

decision_mount=()
if [ -n "$lifecycle_decision" ]; then
	lifecycle_decision="$(realpath "$lifecycle_decision")"
	[ -d "$lifecycle_decision" ] || {
		dkc::warn "lifecycle decision handoff is incomplete"
		false
	}
	decision_mount=(--volume "${lifecycle_decision}:/input/lifecycle-decision:ro")
fi

name="dkc-apt-sign-${DKC_RUN_ID}"
dkc::register_resource container "$name"
log="$stage/output/evidence/orchestration.log"
status=FAIL
failure_phase=sign
if dkc::archive_worktree |
	podman run --rm --interactive --network=none --read-only \
		--read-only-tmpfs=false --userns=keep-id --name "$name" \
		--label "${DKC_LABEL_NS}=${DKC_RUN_ID}" \
		--label "${DKC_LABEL_OWNER}=${DKC_OWNER_ID}" \
		--cap-drop=ALL --security-opt=no-new-privileges \
		--no-hosts --ipc=private --pid=private --uts=private --cgroupns=private \
		--pids-limit=1024 --umask=077 --log-driver=none \
		--env HOME=/work/home --env LC_ALL=C.UTF-8 --env LANG=C.UTF-8 --env TZ=UTC \
		--env PYTHONDONTWRITEBYTECODE=1 \
		--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=128m,mode=1777 \
		--tmpfs=/work:rw,exec,nosuid,nodev,size=512m,mode=1777 \
		--volume "${unsigned}:/input/unsigned:ro" \
		--volume "${keys_dir}:/input/keys:ro" \
		--volume "${secret_dir}:/run/dkc-secrets:ro" \
		"${decision_mount[@]}" \
		--volume "${stage}/output:/output:rw" \
		--workdir /work "$toolbox" sh -ceu '
		test "$(id -u)" -ne 0
		grep -Eq "^CapEff:[[:space:]]+0+$" /proc/self/status
		grep -Eq "^NoNewPrivs:[[:space:]]+1$" /proc/self/status
		mkdir -p /work/repo "$HOME"
		tar --extract --file=- --directory=/work/repo
		cd /work/repo
		decision_state="$1"
		shift
		if [ "$decision_state" = present ]; then
			set -- --lifecycle-decision /input/lifecycle-decision "$@"
		fi
		exec python3 scripts/in-container/build-signed-repository.py sign \
			--unsigned /input/unsigned/repository \
			--request /input/unsigned/handoff/signing-request.json \
			--public-keyring /input/keys/dkc-archive-keyring.gpg \
			--primary-fingerprint /input/keys/archive-primary.fingerprint \
			--signing-subkeys /input/keys/archive-signing-subkeys.fingerprints \
			--secret-subkey /run/dkc-secrets/subkey.gpg \
			--passphrase-file /run/dkc-secrets/passphrase \
			--gpg-home /work/gnupg --output /output/overlay "$@"
	' sh "$(if [ -n "$lifecycle_decision" ]; then printf present; else printf absent; fi)" \
		--clock-skew-seconds "$clock_skew_seconds" \
		--safety-seconds "$safety_seconds" >"$stage/output/evidence/signing.json" 2>"$log"; then
	status=PASS
else
	rc=$?
	tail -n 120 "$log" >&2 || true
fi

if find "$stage/output" -type f \
	\( -name 'subkey.gpg' -o -name 'passphrase' -o -name 'private-keys-v1.d' \) \
	-print -quit | grep -q .; then
	dkc::die "private signing material escaped the signing workspace"
fi
if [ "$status" = PASS ]; then
	cat >"$stage/output/evidence/result.env" <<EOF
status=PASS
repository_signing=PASS
signing_mode=${signing_mode}
signing_subkey_isolated=true
publishable=false
EOF
else
	cat >"$stage/output/evidence/result.env" <<EOF
status=FAIL
repository_signing=FAIL
failure_phase=sign
signing_mode=${signing_mode}
publishable=false
EOF
fi
(
	cd "$stage/output"
	find . -type f ! -path './evidence/evidence.sha256' -print0 |
		sort -z | xargs -0 -r sha256sum
) >"$stage/output/evidence/evidence.sha256"

mkdir -p "$output_root"
[ ! -e "$output" ] || dkc::die "refusing to replace APT signature output: $output"
trap - ERR
mv "$stage/output" "$output"
if [ "$status" = PASS ]; then
	ln -sfn "$DKC_RUN_ID" "$output_root/latest"
	dkc::ok "APT signature overlay complete: $output"
else
	ln -sfn "$DKC_RUN_ID" "$output_root/latest-failed"
	dkc::die "APT repository signing failed; evidence: $output"
fi

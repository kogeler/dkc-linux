#!/usr/bin/env bash
# Dispatch the common APT repository flow for local runs and CI handoffs.

# shellcheck source=scripts/lib/common.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"

dkc::refuse_root
dkc::require_cmd podman tar realpath base64

[ "$#" -eq 16 ] || dkc::die \
	"usage: apt-repository.sh <phase> <toolbox> <apt-client> <matrix> <unsigned> <signature> <keys> <epoch> <generation> <clock-skew> <safety> <previous-pool-result-or-empty> <previous-state-result-or-empty> <retention-mode> <retention-max-bytes-or-empty> <lifecycle-decision-or-empty>"
phase="$1"
toolbox="$2"
client_image="$3"
matrix="$4"
unsigned="$5"
signature="$6"
keys_dir="$7"
epoch="$8"
generation="$9"
clock_skew="${10}"
safety="${11}"
previous_pool_result="${12}"
previous_state_result="${13}"
retention_mode="${14}"
retention_max_bytes="${15}"
lifecycle_decision="${16}"

case "$phase" in
assemble | sign | verify | all) ;;
*) dkc::die "APT_REPOSITORY_PHASE must be assemble, sign, verify, or all" ;;
esac
case "${DKC_APT_EPHEMERAL_SIGNING:-0}" in
0 | 1) ;;
*) dkc::die "DKC_APT_EPHEMERAL_SIGNING must be exactly 0 or 1" ;;
esac
if [ "${DKC_APT_EPHEMERAL_SIGNING:-0}" = 1 ] && [ -n "${GITHUB_ACTIONS:-}" ]; then
	dkc::die "ephemeral APT signing is forbidden in GitHub Actions"
fi
if [ "$phase" = all ] && [ -n "${GITHUB_ACTIONS:-}" ]; then
	dkc::die "GitHub Actions must keep APT assembly, signing, and verification in separate jobs"
fi

ephemeral_keys=""
cleanup_ephemeral_keys() {
	if [[ -n "$ephemeral_keys" && "$ephemeral_keys" == /tmp/dkc-apt-keys.* ]]; then
		rm -rf -- "$ephemeral_keys"
	fi
}
trap cleanup_ephemeral_keys EXIT

generate_ephemeral_keys() {
	podman image exists "$toolbox" || dkc::die "toolbox image is missing; run: make image"
	ephemeral_keys="$(mktemp -d /tmp/dkc-apt-keys.XXXXXXXX)"
	chmod 0700 "$ephemeral_keys"
	local name="dkc-apt-ephemeral-key-${DKC_RUN_ID}"
	if ! dkc::archive_worktree |
		podman run --rm --interactive --network=none --read-only \
			--read-only-tmpfs=false --userns=keep-id --name "$name" \
			--cap-drop=ALL --security-opt=no-new-privileges --pids-limit=256 \
			--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=64m,mode=1777 \
			--tmpfs=/work:rw,exec,nosuid,nodev,size=256m,mode=1777 \
			--volume "$ephemeral_keys:/output:rw" \
			--workdir /work --env HOME=/work/home "$toolbox" sh -ceu '
			test "$(id -u)" -ne 0
			grep -Eq "^CapEff:[[:space:]]+0+$" /proc/self/status
			grep -Eq "^NoNewPrivs:[[:space:]]+1$" /proc/self/status
			mkdir -p /work/repo "$HOME"
			tar --extract --file=- --directory=/work/repo
			cd /work/repo
			exec scripts/in-container/generate-ephemeral-archive-key.sh /output
		'; then
		dkc::die "ephemeral archive key generation failed"
	fi
	keys_dir="$ephemeral_keys"
	APT_GPG_SIGNING_SUBKEY_B64="$(base64 -w0 "$ephemeral_keys/signing-subkey.gpg")"
	APT_GPG_PASSPHRASE="$(<"$ephemeral_keys/passphrase")"
	export APT_GPG_SIGNING_SUBKEY_B64 APT_GPG_PASSPHRASE
}

if [ "$phase" = all ] && [ "${DKC_APT_EPHEMERAL_SIGNING:-0}" = 1 ]; then
	generate_ephemeral_keys
fi

case "$phase" in
assemble)
	exec "$DKC_ROOT/scripts/assemble-apt-repository.sh" \
		"$toolbox" "$matrix" "$keys_dir" "$epoch" "$generation" \
		"$previous_pool_result" "$previous_state_result" \
		"$retention_mode" "$retention_max_bytes"
	;;
sign)
	exec "$DKC_ROOT/scripts/sign-apt-repository.sh" \
		"$toolbox" "$unsigned" "$keys_dir" "$clock_skew" "$safety" \
		"$lifecycle_decision"
	;;
verify)
	exec "$DKC_ROOT/scripts/verify-apt-repository.sh" \
		"$toolbox" "$client_image" "$unsigned" "$signature" "$keys_dir"
	;;
all)
	"$DKC_ROOT/scripts/assemble-apt-repository.sh" \
		"$toolbox" "$matrix" "$keys_dir" "$epoch" "$generation" \
		"$previous_pool_result" "$previous_state_result" \
		"$retention_mode" "$retention_max_bytes"
	unsigned="$DKC_ROOT/out/apt-unsigned/$DKC_RUN_ID"
	"$DKC_ROOT/scripts/sign-apt-repository.sh" \
		"$toolbox" "$unsigned" "$keys_dir" "$clock_skew" "$safety" \
		"$lifecycle_decision"
	signature="$DKC_ROOT/out/apt-signature/$DKC_RUN_ID"
	"$DKC_ROOT/scripts/verify-apt-repository.sh" \
		"$toolbox" "$client_image" "$unsigned" "$signature" "$keys_dir"
	;;
esac

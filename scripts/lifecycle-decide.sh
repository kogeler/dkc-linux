#!/usr/bin/env bash
# Produce one deterministic lifecycle decision from two verified handoffs.

# shellcheck source=scripts/lib/common.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"

dkc::refuse_root
dkc::require_cmd podman realpath tar
dkc::install_cleanup_trap

[ "$#" -eq 9 ] || dkc::die \
	"usage: lifecycle-decide.sh <toolbox> <source-result> <state-result> <epoch> <bootstrap-allowed> <dkc-revision> <lto-mode> <retention-mode> <retention-max-bytes-or-empty>"
toolbox="$1"
source_result="$(realpath "$2")"
state_result="$(realpath "$3")"
epoch="$4"
bootstrap_allowed="$5"
dkc_revision="$6"
lto_mode="$7"
retention_mode="$8"
retention_max_bytes="$9"
[[ "$epoch" =~ ^[1-9][0-9]*$ ]] || dkc::die "decision epoch must be positive"
[[ "$dkc_revision" =~ ^[1-9][0-9]*$ ]] || dkc::die "DKC revision must be positive"
case "$bootstrap_allowed" in
true | false) ;;
*) dkc::die "bootstrap permission must be exactly true or false" ;;
esac
case "$lto_mode" in none | thin | full) ;; *) dkc::die "LTO mode must be none, thin, or full" ;; esac
case "$retention_mode" in
series) [ -z "$retention_max_bytes" ] || dkc::die "series retention does not accept a byte limit" ;;
series-size) [[ "$retention_max_bytes" =~ ^[1-9][0-9]*$ ]] || dkc::die "size retention requires a positive byte limit" ;;
*) dkc::die "retention mode must be series or series-size" ;;
esac

output_root="$DKC_ROOT/out/lifecycle-decision"
output="$output_root/$DKC_RUN_ID"
mkdir -p "$output_root"
[ ! -e "$output" ] || dkc::die "refusing to replace lifecycle decision output"
name="dkc-lifecycle-decision-${DKC_RUN_ID}"
dkc::register_resource container "$name"
if ! dkc::archive_worktree |
	podman run --rm --interactive --network=none --read-only --read-only-tmpfs=false \
		--userns=keep-id --name "$name" \
		--label "${DKC_LABEL_NS}=${DKC_RUN_ID}" \
		--label "${DKC_LABEL_OWNER}=${DKC_OWNER_ID}" \
		--cap-drop=ALL --security-opt=no-new-privileges \
		--no-hosts --ipc=private --pid=private --uts=private --cgroupns=private \
		--pids-limit=256 --umask=077 --log-driver=none \
		--env HOME=/tmp/home --env LC_ALL=C.UTF-8 --env LANG=C.UTF-8 --env TZ=UTC \
		--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=64m,mode=1777 \
		--tmpfs=/work:rw,exec,nosuid,nodev,size=128m,mode=1777 \
		--volume "$source_result:/input/source:ro" \
		--volume "$state_result:/input/state:ro" \
		--volume "$output_root:/output:rw" \
		--workdir /work "$toolbox" sh -ceu '
		test "$(id -u)" -ne 0
		mkdir -p /work/repo "$HOME"
		tar --extract --file=- --directory=/work/repo
		cd /work/repo
		export PYTHONPATH=/work/repo
		exec python3 scripts/in-container/lifecycle-decide.py \
			--source /input/source --state /input/state --output "/output/$1" \
			--keyring /work/repo/keys/dkc-archive-keyring.gpg \
			--signing-subkeys /work/repo/keys/archive-signing-subkeys.fingerprints \
			--epoch "$2" --bootstrap-allowed "$3" \
			--dkc-revision "$4" --lto-mode "$5" \
			--retention-mode "$6" --retention-max-bytes "$7"
	' sh "$DKC_RUN_ID" "$epoch" "$bootstrap_allowed" "$dkc_revision" "$lto_mode" \
		"$retention_mode" "$retention_max_bytes"; then
	dkc::die "lifecycle decision failed"
fi
(
	cd "$output"
	sha256sum --check evidence.sha256 >/dev/null
)
ln -sfn "$DKC_RUN_ID" "$output_root/latest"
dkc::ok "lifecycle decision passed"

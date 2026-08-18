#!/usr/bin/env bash
# Assemble one unsigned binary/source APT repository without access to secrets.

# shellcheck source=scripts/lib/common.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"

dkc::refuse_root
dkc::require_cmd podman tar realpath
dkc::install_cleanup_trap

[ "$#" -eq 9 ] || dkc::die \
	"usage: assemble-apt-repository.sh <toolbox> <matrix-result> <keys-dir> <epoch> <generation> <previous-pool-result-or-empty> <previous-state-result-or-empty> <retention-mode> <retention-max-bytes-or-empty>"
toolbox="$1"
provided_matrix="$2"
matrix=""
matrix_volumes=()
matrix_arg=""
if [ -n "$provided_matrix" ]; then
	matrix="$(realpath "$provided_matrix")"
	matrix_volumes=(--volume "${matrix}:/input/matrix:ro")
	matrix_arg=/input/matrix
fi
keys_dir="$(realpath "$3")"
epoch="$4"
generation="$5"
previous_pool_result="$6"
previous_state_result="$7"
retention_mode="$8"
retention_max_bytes="$9"
[[ "$epoch" =~ ^[1-9][0-9]*$ ]] || dkc::die "repository epoch must be positive"
[[ "$generation" =~ ^[0-9]+$ ]] || dkc::die "repository generation must be non-negative"
case "$retention_mode" in
series) [ -z "$retention_max_bytes" ] || dkc::die "series retention does not accept a byte limit" ;;
series-size) [[ "$retention_max_bytes" =~ ^[1-9][0-9]*$ ]] || dkc::die "size retention requires a positive byte limit" ;;
*) dkc::die "retention mode must be series or series-size" ;;
esac
previous_volumes=()
previous_args=("" "")
if [ -n "$previous_pool_result" ] || [ -n "$previous_state_result" ]; then
	if [ -z "$previous_pool_result" ] || [ -z "$previous_state_result" ]; then
		dkc::die "previous pool and signed state must be supplied together"
	fi
	previous_pool_result="$(realpath "$previous_pool_result")"
	previous_state_result="$(realpath "$previous_state_result")"
	if [ ! -d "$previous_pool_result/pool" ] || [ -L "$previous_pool_result/pool" ]; then
		dkc::die "previous live-pool export is incomplete"
	fi
	if [ ! -f "$previous_state_result/evidence.sha256" ] ||
		[ -L "$previous_state_result/evidence.sha256" ]; then
		dkc::die "previous signed-state export is incomplete"
	fi
	previous_volumes=(
		--volume "${previous_pool_result}:/input/previous-pool-result:ro"
		--volume "${previous_state_result}:/input/previous-state-result:ro"
	)
	previous_args=("/input/previous-pool-result" "/input/previous-state-result")
fi
if [ -z "$matrix" ] && [ -z "$previous_pool_result" ]; then
	dkc::die "metadata maintenance requires the previous live pool and signed state"
fi
for path in \
	keys/dkc-archive-keyring.gpg \
	keys/archive-primary.fingerprint \
	keys/archive-signing-subkeys.fingerprints; do
	if [ ! -f "$keys_dir/${path#keys/}" ] || [ -L "$keys_dir/${path#keys/}" ]; then
		dkc::die "missing tracked archive key material: ${path}; follow docs/KEYS.md"
	fi
done
if [ -n "$matrix" ] &&
	{ [ ! -d "$matrix/flat-repository" ] || [ ! -d "$matrix/evidence" ]; }; then
	dkc::die "package-matrix result is incomplete: $matrix"
fi
podman image exists "$toolbox" || dkc::die "toolbox image is missing; run: make image"
[ "$(podman info --format '{{.Host.Security.Rootless}}' 2>/dev/null)" = true ] ||
	dkc::die "rootless podman is required"

stage="$DKC_RUN_DIR/apt-unsigned"
mkdir -p "$stage/output/evidence"
dkc::register_resource path "$stage"
name="dkc-apt-assemble-${DKC_RUN_ID}"
dkc::register_resource container "$name"
log="$stage/output/evidence/orchestration.log"
status=FAIL
if dkc::archive_worktree |
	podman run --rm --interactive --network=none --read-only \
		--read-only-tmpfs=false --userns=keep-id --name "$name" \
		--label "${DKC_LABEL_NS}=${DKC_RUN_ID}" \
		--label "${DKC_LABEL_OWNER}=${DKC_OWNER_ID}" \
		--cap-drop=ALL --security-opt=no-new-privileges \
		--no-hosts --ipc=private --pid=private --uts=private --cgroupns=private \
		--pids-limit=8192 --umask=077 --log-driver=none \
		--env HOME=/work/home --env LC_ALL=C.UTF-8 --env LANG=C.UTF-8 --env TZ=UTC \
		--env PYTHONDONTWRITEBYTECODE=1 \
		--tmpfs=/tmp:rw,exec,nosuid,nodev,size=1g,mode=1777 \
		--tmpfs=/work:rw,exec,nosuid,nodev,size=2g,mode=1777 \
		"${matrix_volumes[@]}" \
		--volume "${keys_dir}:/input/keys:ro" \
		"${previous_volumes[@]}" \
		--volume "${stage}/output:/output:rw" \
		--workdir /work "$toolbox" sh -ceu '
		test "$(id -u)" -ne 0
		grep -Eq "^CapEff:[[:space:]]+0+$" /proc/self/status
		grep -Eq "^NoNewPrivs:[[:space:]]+1$" /proc/self/status
		mkdir -p /work/repo "$HOME"
		tar --extract --file=- --directory=/work/repo
		cd /work/repo
		exec scripts/in-container/assemble-apt-repository.sh \
			"$1" /output \
			/input/keys/dkc-archive-keyring.gpg \
			/input/keys/archive-primary.fingerprint \
			/input/keys/archive-signing-subkeys.fingerprints \
			"$2" "$3" "$4" "$5" "$6" "$7"
	' sh "$matrix_arg" "$epoch" "$generation" "${previous_args[0]}" "${previous_args[1]}" \
		"$retention_mode" "$retention_max_bytes" >"$log" 2>&1; then
	status=PASS
else
	rc=$?
	tail -n 160 "$log" >&2 || true
fi

if [ "$status" = FAIL ]; then
	cat >"$stage/output/evidence/result.env" <<EOF
status=FAIL
repository_assembly=FAIL
publishable=false
EOF
fi
(
	cd "$stage/output"
	find . -type f ! -path './evidence/evidence.sha256' -print0 |
		sort -z | xargs -0 -r sha256sum
) >"$stage/output/evidence/evidence.sha256"

output_root="$DKC_ROOT/out/apt-unsigned"
output="$output_root/$DKC_RUN_ID"
mkdir -p "$output_root"
[ ! -e "$output" ] || dkc::die "refusing to replace APT assembly output: $output"
mv "$stage/output" "$output"
if [ "$status" = PASS ]; then
	ln -sfn "$DKC_RUN_ID" "$output_root/latest"
	dkc::ok "unsigned APT repository complete: $output"
else
	ln -sfn "$DKC_RUN_ID" "$output_root/latest-failed"
	dkc::die "unsigned APT repository assembly failed; evidence: $output"
fi

#!/usr/bin/env bash
# Replay a saved repository with no external network or storage credentials.

# shellcheck source=scripts/lib/common.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"

dkc::refuse_root
dkc::require_cmd podman realpath tar
dkc::install_cleanup_trap
[ "$#" -eq 2 ] || dkc::die "usage: storage-publication-replay.sh <toolbox> <repository-result>"
toolbox="$1"
repository_result="$(realpath "$2")"
[ -d "$repository_result/repository" ] || dkc::die "verified repository result is incomplete"
[ "$(podman info --format '{{.Host.Security.Rootless}}' 2>/dev/null)" = true ] ||
	dkc::die "rootless podman is required"
output_root="$DKC_ROOT/out/storage-publication-replay"
mkdir -p "$output_root"
output="$output_root/$DKC_RUN_ID"
[ ! -e "$output" ] || dkc::die "refusing to replace replay evidence"
name="dkc-storage-replay-${DKC_RUN_ID}"
dkc::register_resource container "$name"
dkc::archive_worktree |
	podman run --rm --interactive --read-only --read-only-tmpfs=false \
		--network=none --userns=keep-id --name "$name" \
		--label "${DKC_LABEL_NS}=${DKC_RUN_ID}" --label "${DKC_LABEL_OWNER}=${DKC_OWNER_ID}" \
		--cap-drop=ALL --security-opt=no-new-privileges --pids-limit=512 --umask=077 \
		--log-driver=none --ipc=private --pid=private --uts=private --cgroupns=private \
		--env HOME=/tmp/home --env LC_ALL=C.UTF-8 --env LANG=C.UTF-8 --env TZ=UTC \
		--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=256m,mode=1777 \
		--tmpfs=/work:rw,exec,nosuid,nodev,size=512m,mode=1777 \
		--volume "$repository_result:/input/repository-result:ro" \
		--volume "$output_root:/output:rw" --workdir /work "$toolbox" sh -ceu '
		test "$(id -u)" -ne 0
		grep -Eq "^CapEff:[[:space:]]+0+$" /proc/self/status
		grep -Eq "^NoNewPrivs:[[:space:]]+1$" /proc/self/status
		mkdir -p /work/repo "$HOME"
		tar --extract --file=- --directory=/work/repo
		cd /work/repo
		export PYTHONPATH=/work/repo
		exec python3 -m tests.storage_publication_replay \
			--repository-result /input/repository-result --output "/output/$1"
	' sh "$DKC_RUN_ID"
(
	cd "$output"
	sha256sum --check evidence.sha256 >/dev/null
)
ln -sfn "$DKC_RUN_ID" "$output_root/latest"
dkc::ok "local HTTPS publication replay passed"

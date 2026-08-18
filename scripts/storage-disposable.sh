#!/usr/bin/env bash
# Exercise a verified repository below one exact disposable storage prefix.

# shellcheck source=scripts/lib/common.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"
# shellcheck source=scripts/lib/storage-connection.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib/storage-connection.sh"

dkc::refuse_root
dkc::require_cmd podman python3 realpath tar
dkc::install_cleanup_trap

[ "$#" -eq 3 ] || dkc::die \
	"usage: storage-disposable.sh <toolbox-image> <verified-repository-result> <connection-file-or-empty>"
toolbox="$1"
repository_result="$(realpath "$2")"
provided_connection="$3"
[ -d "$repository_result/repository" ] || dkc::die \
	"verified repository result is incomplete: $repository_result"
podman image exists "$toolbox" || dkc::die "toolbox image is missing; run: make image"
[ "$(podman info --format '{{.Host.Security.Rootless}}' 2>/dev/null)" = true ] ||
	dkc::die "rootless podman is required"

stage="$DKC_RUN_DIR/storage-disposable"
dkc::register_resource path "$stage"
export DKC_CANONICAL_REPOSITORY="${DKC_CANONICAL_REPOSITORY:-}"
connection_file=""
dkc::prepare_storage_connection "$stage" "$provided_connection" connection_file

output_root="$DKC_ROOT/out/storage-disposable"
output="$output_root/$DKC_RUN_ID"
mkdir -p "$output_root"
[ ! -e "$output" ] || dkc::die "refusing to replace disposable evidence: $output"

container_name="dkc-storage-disposable-${DKC_RUN_ID}"
dkc::register_resource container "$container_name"
network_flags=()
if [ -n "${DKC_PODMAN_NETWORK:-}" ]; then
	network_flags+=(--network="$DKC_PODMAN_NETWORK")
fi

dkc::info "qualifying the verified repository under one disposable storage prefix"
if dkc::archive_worktree |
	podman run --rm --interactive --read-only --read-only-tmpfs=false \
		--userns=keep-id --name "$container_name" \
		--label "${DKC_LABEL_NS}=${DKC_RUN_ID}" \
		--label "${DKC_LABEL_OWNER}=${DKC_OWNER_ID}" \
		--cap-drop=ALL --security-opt=no-new-privileges \
		--no-hosts --ipc=private --pid=private --uts=private --cgroupns=private \
		--pids-limit=1024 --umask=077 --log-driver=none \
		--env HOME=/tmp/home --env LC_ALL=C.UTF-8 --env LANG=C.UTF-8 --env TZ=UTC \
		--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=128m,mode=1777 \
		--tmpfs=/work:rw,exec,nosuid,nodev,size=512m,mode=1777 \
		--volume "$repository_result:/input/repository-result:ro" \
		--volume "$connection_file:/run/secrets/storage.json:ro" \
		--volume "$output_root:/output:rw" \
		"${network_flags[@]}" \
		--workdir /work "$toolbox" sh -ceu '
		test "$(id -u)" -ne 0
		grep -Eq "^CapEff:[[:space:]]+0+$" /proc/self/status
		grep -Eq "^NoNewPrivs:[[:space:]]+1$" /proc/self/status
		mkdir -p /work/repo "$HOME"
		tar --extract --file=- --directory=/work/repo
		cd /work/repo
		export PYTHONPATH=/work/repo
		exec python3 scripts/in-container/storage-disposable.py \
			--repository-result /input/repository-result \
			--connection /run/secrets/storage.json \
			--output "/output/$1" \
			--run-id "$1" \
			--canonical-repository "$2"
	' sh "$DKC_RUN_ID" "$DKC_CANONICAL_REPOSITORY"; then
	rc=0
else
	rc=$?
fi

[ -d "$output" ] || dkc::die "disposable integration produced no durable result"
if [ -f "$output/evidence.sha256" ]; then
	(
		cd "$output"
		sha256sum --check evidence.sha256 >/dev/null
	)
else
	dkc::warn "integration stopped without final evidence; run storage-disposable-cleanup for the retained result"
	rc=1
fi
case "$rc" in
0)
	ln -sfn "$DKC_RUN_ID" "$output_root/latest"
	dkc::ok "disposable storage integration passed with zero leftovers"
	;;
2)
	ln -sfn "$DKC_RUN_ID" "$output_root/latest-blocked"
	dkc::warn "disposable storage integration blocked before mutation"
	;;
*)
	ln -sfn "$DKC_RUN_ID" "$output_root/latest-failed"
	dkc::warn "disposable storage integration failed with retained evidence"
	;;
esac
trap - ERR
exit "$rc"

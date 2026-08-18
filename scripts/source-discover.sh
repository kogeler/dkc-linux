#!/usr/bin/env bash
# Resolve one authenticated source inventory without mutating the host.

# shellcheck source=scripts/lib/common.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"

dkc::refuse_root
dkc::require_cmd podman tar
dkc::install_cleanup_trap

[ "$#" -eq 2 ] || dkc::die "usage: source-discover.sh <toolbox-image> <epoch>"
toolbox="$1"
epoch="$2"
[[ "$epoch" =~ ^[1-9][0-9]*$ ]] || dkc::die "source discovery epoch must be positive"
podman image exists "$toolbox" || dkc::die "toolbox image is missing; run: make image"

output_root="$DKC_ROOT/out/source-discovery"
output="$output_root/$DKC_RUN_ID"
mkdir -p "$output_root"
[ ! -e "$output" ] || dkc::die "refusing to replace source discovery output"
name="dkc-source-discovery-${DKC_RUN_ID}"
dkc::register_resource container "$name"

if ! dkc::archive_worktree |
	podman run --rm --interactive --read-only --read-only-tmpfs=false \
		--userns=keep-id --name "$name" \
		--label "${DKC_LABEL_NS}=${DKC_RUN_ID}" \
		--label "${DKC_LABEL_OWNER}=${DKC_OWNER_ID}" \
		--cap-drop=ALL --security-opt=no-new-privileges \
		--ipc=private --pid=private --uts=private --cgroupns=private \
		--pids-limit=512 --umask=077 --log-driver=none \
		--env HOME=/tmp/home --env LC_ALL=C.UTF-8 --env LANG=C.UTF-8 --env TZ=UTC \
		--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=512m,mode=1777 \
		--tmpfs=/work:rw,exec,nosuid,nodev,size=512m,mode=1777 \
		--volume "$output_root:/output:rw" \
		--workdir /work "$toolbox" sh -ceu '
		test "$(id -u)" -ne 0
		grep -Eq "^CapEff:[[:space:]]+0+$" /proc/self/status
		grep -Eq "^NoNewPrivs:[[:space:]]+1$" /proc/self/status
		mkdir -p /work/repo "$HOME"
		tar --extract --file=- --directory=/work/repo
		cd /work/repo
		export PYTHONPATH=/work/repo
		exec python3 scripts/in-container/discover-source.py \
			--output "/output/$1" --epoch "$2"
	' sh "$DKC_RUN_ID" "$epoch"; then
	dkc::die "authenticated source discovery failed"
fi
(
	cd "$output"
	sha256sum --check evidence.sha256 >/dev/null
)
ln -sfn "$DKC_RUN_ID" "$output_root/latest"
dkc::ok "authenticated source discovery passed"

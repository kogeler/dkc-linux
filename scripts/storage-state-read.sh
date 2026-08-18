#!/usr/bin/env bash
# Read authoritative signed state using one read-only storage credential.

# shellcheck source=scripts/lib/common.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"
# shellcheck source=scripts/lib/storage-connection.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib/storage-connection.sh"

dkc::refuse_root
dkc::require_cmd podman python3 tar
dkc::install_cleanup_trap
umask 077

[ "$#" -eq 3 ] || dkc::die \
	"usage: storage-state-read.sh <toolbox-image> <keys-dir> <connection-file-or-empty>"
toolbox="$1"
keys_dir="$(realpath "$2")"
provided_connection="$3"
podman image exists "$toolbox" || dkc::die "toolbox image is missing; run: make image"
for key in dkc-archive-keyring.gpg archive-signing-subkeys.fingerprints; do
	if [ ! -f "$keys_dir/$key" ] || [ -L "$keys_dir/$key" ]; then
		dkc::die "tracked public key material is incomplete"
	fi
done

stage="$DKC_RUN_DIR/storage-state-read"
dkc::register_resource path "$stage"
connection_file=""
dkc::prepare_storage_connection "$stage" "$provided_connection" connection_file

output_root="$DKC_ROOT/out/authoritative-state"
output="$output_root/$DKC_RUN_ID"
mkdir -p "$output_root"
[ ! -e "$output" ] || dkc::die "refusing to replace authoritative state output"
name="dkc-state-read-${DKC_RUN_ID}"
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
		--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=128m,mode=1777 \
		--tmpfs=/work:rw,exec,nosuid,nodev,size=256m,mode=1777 \
		--volume "$connection_file:/run/secrets/storage.json:ro" \
		--volume "$keys_dir:/input/keys:ro" \
		--volume "$output_root:/output:rw" \
		--workdir /work "$toolbox" sh -ceu '
		test "$(id -u)" -ne 0
		grep -Eq "^CapEff:[[:space:]]+0+$" /proc/self/status
		grep -Eq "^NoNewPrivs:[[:space:]]+1$" /proc/self/status
		mkdir -p /work/repo "$HOME"
		tar --extract --file=- --directory=/work/repo
		cd /work/repo
		export PYTHONPATH=/work/repo
		exec python3 scripts/in-container/storage-state-read.py \
			--connection /run/secrets/storage.json \
			--keyring /input/keys/dkc-archive-keyring.gpg \
			--signing-subkeys /input/keys/archive-signing-subkeys.fingerprints \
			--output "/output/$1"
	' sh "$DKC_RUN_ID"; then
	dkc::die "authoritative state read failed"
fi
(
	cd "$output"
	sha256sum --check evidence.sha256 >/dev/null
)
ln -sfn "$DKC_RUN_ID" "$output_root/latest"
dkc::ok "authoritative state read passed"

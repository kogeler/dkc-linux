#!/usr/bin/env bash
# Commit one signed repository to production-compatible S3 storage.

# shellcheck source=scripts/lib/common.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"
# shellcheck source=scripts/lib/storage-connection.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib/storage-connection.sh"

dkc::refuse_root
dkc::require_cmd podman python3 realpath tar
dkc::install_cleanup_trap
umask 077

[ "$#" -eq 13 ] || dkc::die \
	"usage: storage-publish.sh <toolbox> <repository-result> <keys> <connection-or-empty> <canonical-repository> <expected-commit> <workflow-run-id> <run-attempt> <max-object-bytes> <lease-ttl> <takeover-grace> <gc-max-objects> <gc-max-bytes>"
toolbox="$1"
repository_result="$(realpath "$2")"
keys_dir="$(realpath "$3")"
provided_connection="$4"
canonical_repository="$5"
expected_commit="$6"
workflow_run_id="$7"
run_attempt="$8"
max_object_bytes="$9"
lease_ttl="${10}"
takeover_grace="${11}"
gc_max_objects="${12}"
gc_max_bytes="${13}"

[[ "$canonical_repository" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] ||
	dkc::die "unsafe canonical repository"
[[ "$expected_commit" =~ ^[0-9a-f]{40}$ ]] || dkc::die "expected commit is not a full Git SHA"
[[ "$workflow_run_id" =~ ^[1-9][0-9]*$ ]] || dkc::die "workflow run ID is invalid"
[[ "$run_attempt" =~ ^[1-9][0-9]*$ ]] || dkc::die "workflow run attempt is invalid"
for value in "$max_object_bytes" "$lease_ttl" "$takeover_grace" "$gc_max_objects" "$gc_max_bytes"; do
	[[ "$value" =~ ^[0-9]+$ ]] || dkc::die "storage publication bound is invalid"
done
[ "$max_object_bytes" -gt 0 ] || dkc::die "storage object-size limit must be positive"
[ "$lease_ttl" -ge 300 ] || dkc::die "storage lease TTL must be at least 300 seconds"
if [ "$gc_max_objects" -le 0 ] || [ "$gc_max_bytes" -le 0 ]; then
	dkc::die "storage GC caps must be positive"
fi

# This is the last no-secret network check. A stale commit never reaches the
# storage container or its credential.
env \
	-u S3_ENDPOINT -u S3_REGION -u S3_BUCKET -u S3_ADDRESSING_STYLE \
	-u S3_ACCESS_KEY_ID -u S3_SECRET_ACCESS_KEY -u S3_SESSION_TOKEN \
	-u GITHUB_TOKEN \
	"$DKC_ROOT/scripts/check-current-main.sh" "$canonical_repository" "$expected_commit"
[ -d "$repository_result/repository" ] || dkc::die "verified repository result is incomplete"
for key in dkc-archive-keyring.gpg archive-signing-subkeys.fingerprints; do
	if [ ! -f "$keys_dir/$key" ] || [ -L "$keys_dir/$key" ]; then
		dkc::die "tracked public key material is incomplete"
	fi
done
podman image exists "$toolbox" || dkc::die "toolbox image is missing; run: make image"

stage="$DKC_RUN_DIR/storage-publish"
dkc::register_resource path "$stage"
connection_file=""
dkc::prepare_storage_connection "$stage" "$provided_connection" connection_file
github_token_file="$stage/github-token"
printf '%s' "${GITHUB_TOKEN:-}" >"$github_token_file"
chmod 0600 "$github_token_file"
unset GITHUB_TOKEN

output_root="$DKC_ROOT/out/storage-publication"
output="$output_root/$DKC_RUN_ID"
mkdir -p "$output_root"
[ ! -e "$output" ] || dkc::die "refusing to replace storage publication output"
name="dkc-storage-publish-${DKC_RUN_ID}"
dkc::register_resource container "$name"
if dkc::archive_worktree |
	podman run --rm --interactive --read-only --read-only-tmpfs=false \
		--userns=keep-id --name "$name" \
		--label "${DKC_LABEL_NS}=${DKC_RUN_ID}" \
		--label "${DKC_LABEL_OWNER}=${DKC_OWNER_ID}" \
		--cap-drop=ALL --security-opt=no-new-privileges \
		--ipc=private --pid=private --uts=private --cgroupns=private \
		--pids-limit=512 --umask=077 --log-driver=none \
		--env HOME=/tmp/home --env LC_ALL=C.UTF-8 --env LANG=C.UTF-8 --env TZ=UTC \
		--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=256m,mode=1777 \
		--tmpfs=/work:rw,exec,nosuid,nodev,size=512m,mode=1777 \
		--volume "$repository_result:/input/repository-result:ro" \
		--volume "$keys_dir:/input/keys:ro" \
		--volume "$connection_file:/run/secrets/storage.json:ro" \
		--volume "$github_token_file:/run/secrets/github-token:ro" \
		--volume "$output_root:/output:rw" \
		--workdir /work "$toolbox" sh -ceu '
		test "$(id -u)" -ne 0
		grep -Eq "^CapEff:[[:space:]]+0+$" /proc/self/status
		grep -Eq "^NoNewPrivs:[[:space:]]+1$" /proc/self/status
		mkdir -p /work/repo "$HOME"
		tar --extract --file=- --directory=/work/repo
		cd /work/repo
		export PYTHONPATH=/work/repo
		exec python3 scripts/in-container/storage-publish.py \
			--repository-result /input/repository-result \
			--connection /run/secrets/storage.json \
			--keyring /input/keys/dkc-archive-keyring.gpg \
			--signing-subkeys /input/keys/archive-signing-subkeys.fingerprints \
			--github-token-file /run/secrets/github-token \
			--output "/output/$1" --canonical-repository "$2" \
			--workflow-run-id "$3" --run-attempt "$4" \
			--max-object-bytes "$5" --lease-ttl-seconds "$6" \
			--takeover-grace-seconds "$7" --gc-max-objects "$8" \
			--gc-max-bytes "$9"
	' sh "$DKC_RUN_ID" "$canonical_repository" "$workflow_run_id" \
		"$run_attempt" "$max_object_bytes" "$lease_ttl" "$takeover_grace" \
		"$gc_max_objects" "$gc_max_bytes"; then
	status=PASS
else
	rc=$?
	status=FAIL
fi
if [ "$status" = PASS ]; then
	(
		cd "$output"
		sha256sum --check evidence.sha256 >/dev/null
	)
	ln -sfn "$DKC_RUN_ID" "$output_root/latest"
	dkc::ok "storage publication passed"
else
	if [ -d "$output" ]; then
		ln -sfn "$DKC_RUN_ID" "$output_root/latest-failed"
	fi
	dkc::err "storage publication failed with sanitized output"
	exit "$rc"
fi

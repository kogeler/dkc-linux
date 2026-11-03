#!/usr/bin/env bash
# Publish one audited documentation-site build below the site/ storage prefix.

# shellcheck source=scripts/lib/common.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"
# shellcheck source=scripts/lib/storage-connection.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib/storage-connection.sh"

dkc::refuse_root
dkc::require_cmd podman python3 realpath sha256sum tar
dkc::install_cleanup_trap
umask 077

[ "$#" -eq 11 ] || dkc::die \
	"usage: site-publish.sh <toolbox> <site-result> <connection-or-empty> <canonical-repository> <expected-commit> <workflow-run-id> <run-attempt> <lease-ttl> <takeover-grace> <lease-wait> <max-deletions>"
toolbox="$1"
site_result="$(realpath "$2")"
provided_connection="$3"
canonical_repository="$4"
expected_commit="$5"
workflow_run_id="$6"
run_attempt="$7"
lease_ttl="$8"
takeover_grace="$9"
lease_wait="${10}"
max_deletions="${11}"

[[ "$canonical_repository" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] ||
	dkc::die "unsafe canonical repository"
[[ "$expected_commit" =~ ^[0-9a-f]{40}$ ]] || dkc::die "expected commit is not a full Git SHA"
[[ "$workflow_run_id" =~ ^[1-9][0-9]*$ ]] || dkc::die "workflow run ID is invalid"
[[ "$run_attempt" =~ ^[1-9][0-9]*$ ]] || dkc::die "workflow run attempt is invalid"
for value in "$lease_ttl" "$takeover_grace" "$lease_wait" "$max_deletions"; do
	[[ "$value" =~ ^[0-9]+$ ]] || dkc::die "site publication bound is invalid"
done
[ "$lease_ttl" -ge 300 ] || dkc::die "storage lease TTL must be at least 300 seconds"

# This is the last no-secret network check. A superseded commit never reaches
# the storage container or its credential.
env \
	-u S3_ENDPOINT -u S3_REGION -u S3_BUCKET -u S3_ADDRESSING_STYLE \
	-u S3_ACCESS_KEY_ID -u S3_SECRET_ACCESS_KEY -u S3_SESSION_TOKEN \
	-u GITHUB_TOKEN \
	"$DKC_ROOT/scripts/check-current-main.sh" "$canonical_repository" "$expected_commit"
[ -d "$site_result/site" ] || dkc::die "site build result is incomplete"
(
	cd "$site_result"
	sha256sum --check --quiet evidence.sha256
) || dkc::die "site build evidence does not verify"
podman image exists "$toolbox" || dkc::die "toolbox image is missing; run: make image"

stage="$DKC_RUN_DIR/site-publish"
dkc::register_resource path "$stage"
connection_file=""
dkc::prepare_storage_connection "$stage" "$provided_connection" connection_file
github_token_file="$stage/github-token"
printf '%s' "${GITHUB_TOKEN:-}" >"$github_token_file"
chmod 0600 "$github_token_file"
unset GITHUB_TOKEN

output_root="$DKC_ROOT/out/site-publication"
output="$output_root/$DKC_RUN_ID"
mkdir -p "$output_root"
[ ! -e "$output" ] || dkc::die "refusing to replace site publication output"
name="dkc-site-publish-${DKC_RUN_ID}"
dkc::register_resource container "$name"
if dkc::archive_worktree |
	podman run --rm --interactive --read-only --read-only-tmpfs=false \
		--userns=keep-id --name "$name" \
		--label "${DKC_LABEL_NS}=${DKC_RUN_ID}" \
		--label "${DKC_LABEL_OWNER}=${DKC_OWNER_ID}" \
		--cap-drop=ALL --security-opt=no-new-privileges \
		--ipc=private --pid=private --uts=private --cgroupns=private \
		--pids-limit=256 --umask=077 --log-driver=none \
		--env HOME=/tmp/home --env LC_ALL=C.UTF-8 --env LANG=C.UTF-8 --env TZ=UTC \
		--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=64m,mode=1777 \
		--tmpfs=/work:rw,noexec,nosuid,nodev,size=256m,mode=1777 \
		--volume "$site_result:/input/site-result:ro" \
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
		exec python3 scripts/in-container/site-publish.py \
			--site-result /input/site-result \
			--connection /run/secrets/storage.json \
			--github-token-file /run/secrets/github-token \
			--output "/output/$1" --canonical-repository "$2" \
			--workflow-run-id "$3" --run-attempt "$4" \
			--lease-ttl-seconds "$5" --takeover-grace-seconds "$6" \
			--lease-wait-seconds "$7" --max-deletions "$8"
	' sh "$DKC_RUN_ID" "$canonical_repository" "$workflow_run_id" \
		"$run_attempt" "$lease_ttl" "$takeover_grace" "$lease_wait" \
		"$max_deletions"; then
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
	dkc::ok "site publication passed"
else
	if [ -d "$output" ]; then
		ln -sfn "$DKC_RUN_ID" "$output_root/latest-failed"
	fi
	dkc::err "site publication failed with sanitized output"
	exit "$rc"
fi

#!/usr/bin/env bash
# Run post-build attestation replay in a confined rootless container.

# shellcheck source=scripts/lib/common.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"

dkc::refuse_root
dkc::require_cmd podman realpath tar
dkc::install_cleanup_trap

[ "$#" -eq 6 ] || dkc::die \
	"usage: reattest-flavor.sh <image> <llvm-major> <flavor> <result> <observations|full> <update-latest>"
image="$1"
llvm_major="$2"
flavor="$3"
result="$(realpath "$4")"
mode="$5"
update_latest="$6"
[[ "$llvm_major" =~ ^[0-9]+$ ]] || dkc::die "invalid LLVM major"
[[ "$flavor" =~ ^v[234]$ ]] || dkc::die "invalid flavor"
case "$mode" in observations | full) ;; *) dkc::die "invalid replay mode" ;; esac
[[ "$update_latest" =~ ^[01]$ ]] || dkc::die "UPDATE_LATEST must be 0 or 1"
if [ ! -d "$result/artifacts" ] || [ ! -d "$result/evidence" ]; then
	dkc::die "compilation result is incomplete: $result"
fi
podman image exists "$image" || dkc::die "build image is missing; run: make build-image"
[ "$(podman info --format '{{.Host.Security.Rootless}}' 2>/dev/null)" = true ] ||
	dkc::die "rootless podman is required"

stage="${DKC_RUN_DIR}/reattest-${flavor}"
mkdir -p "$stage/output"
dkc::register_resource path "$stage"
volume="dkc-reattest-${flavor}-${DKC_RUN_ID}"
podman volume create \
	--label "${DKC_LABEL_NS}=${DKC_RUN_ID}" \
	--label "${DKC_LABEL_OWNER}=${DKC_OWNER_ID}" \
	"$volume" >/dev/null
dkc::register_resource volume "$volume"
container="dkc-reattest-${flavor}-${DKC_RUN_ID}"
dkc::register_resource container "$container"
log="$stage/reattest.log"

status=FAIL
if dkc::archive_worktree |
	podman run --rm --interactive --network=none --read-only \
		--read-only-tmpfs=false --userns=keep-id --name "$container" \
		--label "${DKC_LABEL_NS}=${DKC_RUN_ID}" \
		--label "${DKC_LABEL_OWNER}=${DKC_OWNER_ID}" \
		--cap-drop=ALL --security-opt=no-new-privileges \
		--no-hosts --ipc=private --pid=private --uts=private --cgroupns=private \
		--pids-limit=8192 --umask=077 --log-driver=none \
		--env HOME=/work/home --env LC_ALL=C.UTF-8 --env LANG=C.UTF-8 --env TZ=UTC \
		--env PYTHONDONTWRITEBYTECODE=1 \
		--tmpfs=/tmp:rw,exec,nosuid,nodev,size=2g,mode=1777 \
		--volume "${volume}:/work:rw,U" \
		--volume "${result}:/input/result:ro" \
		--volume "${stage}/output:/output:rw" \
		--workdir /work "$image" sh -ceu '
		test "$(id -u)" -ne 0
		grep -Eq "^CapEff:[[:space:]]+0+$" /proc/self/status
		grep -Eq "^NoNewPrivs:[[:space:]]+1$" /proc/self/status
		mkdir -p /work/repo "$HOME"
		tar --extract --file=- --directory=/work/repo
		exec /work/repo/scripts/in-container/reattest-flavor.sh \
			/input/result /output "$@"
	' sh "$llvm_major" "$flavor" "$mode" >"$log" 2>&1; then
	status=PASS
else
	rc=$?
	tail -n 160 "$log" >&2 || true
fi

mkdir -p "$stage/output/evidence"
cp "$log" "$stage/output/evidence/reattest.log"
if [ "$status" = FAIL ]; then
	cat >"$stage/output/evidence/result.env" <<EOF
status=FAIL
scope=post-build-reattest
flavor=${flavor}
mode=${mode}
publishable=false
EOF
fi
(
	cd "$stage/output/evidence"
	find . -type f ! -name evidence.sha256 -print0 |
		sort -z | xargs -0 -r sha256sum
) >"$stage/output/evidence/evidence.sha256"

output_root="${DKC_ROOT}/out/reattest/${flavor}"
output="${output_root}/${DKC_RUN_ID}"
mkdir -p "$output_root"
test ! -e "$output" || dkc::die "refusing to replace reattestation output: $output"
mv "$stage/output" "$output"
if [ "$status" = PASS ]; then
	if [ "$update_latest" = 1 ]; then
		ln -sfn "$DKC_RUN_ID" "$output_root/latest"
	fi
	dkc::ok "${flavor} post-build reattestation complete: ${output}"
else
	if [ "$update_latest" = 1 ]; then
		ln -sfn "$DKC_RUN_ID" "$output_root/latest-failed"
	fi
	dkc::die "${flavor} post-build reattestation failed: ${output}"
fi

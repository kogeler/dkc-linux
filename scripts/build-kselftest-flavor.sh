#!/usr/bin/env bash
# Build a reusable exact-source kselftest bundle for one accepted flavor.

# shellcheck source=scripts/lib/common.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"

dkc::refuse_root
dkc::require_cmd podman tar realpath
dkc::install_cleanup_trap

[ "$#" -eq 7 ] || dkc::die \
	"usage: build-kselftest-flavor.sh <image> <llvm-major> <flavor> <flavor-result> <profile> <kind> <update-latest>"
image="$1"
llvm_major="$2"
flavor="$3"
flavor_result="$(realpath "$4")"
profile="$(realpath "$5")"
kind="$6"
update_latest="$7"

[[ "$llvm_major" =~ ^[0-9]+$ ]] || dkc::die "invalid LLVM major"
[[ "$flavor" =~ ^v[234]$ ]] || dkc::die "flavor must be v2, v3, or v4"
[[ "$kind" =~ ^[a-z][a-z0-9-]*$ ]] || dkc::die "unsafe kselftest result kind"
[[ "$update_latest" =~ ^[01]$ ]] || dkc::die "UPDATE_LATEST must be 0 or 1"
case "$profile" in
"${DKC_ROOT}/config/"*) ;;
*) dkc::die "kselftest profile must be inside config/" ;;
esac
if [ ! -f "$profile" ] || [ -L "$profile" ]; then
	dkc::die "kselftest profile is not a plain file"
fi
if [ ! -d "$flavor_result/artifacts" ] || [ ! -d "$flavor_result/evidence" ]; then
	dkc::die "accepted flavor result is incomplete"
fi
[ -d "$flavor_result/source" ] ||
	dkc::die "accepted flavor result does not contain its source bundle"
podman image exists "$image" || dkc::die "build image is missing; run: make build-image"
[ "$(podman info --format '{{.Host.Security.Rootless}}' 2>/dev/null)" = true ] ||
	dkc::die "rootless podman is required"

profile_relative="${profile#"${DKC_ROOT}"/}"
stage="${DKC_RUN_DIR}/kselftest-${kind}-${flavor}"
mkdir -p "$stage/output"
dkc::register_resource path "$stage"
volume="dkc-kselftest-${kind}-${flavor}-${DKC_RUN_ID}"
podman volume create \
	--label "${DKC_LABEL_NS}=${DKC_RUN_ID}" \
	--label "${DKC_LABEL_OWNER}=${DKC_OWNER_ID}" \
	"$volume" >/dev/null
dkc::register_resource volume "$volume"
container="dkc-kselftest-${kind}-${flavor}-${DKC_RUN_ID}"
dkc::register_resource container "$container"
log="$stage/kselftest-orchestration.log"

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
		--volume "${flavor_result}:/input/flavor:ro" \
		--volume "${stage}/output:/output:rw" \
		--workdir /work "$image" sh -ceu '
		test "$(id -u)" -ne 0
		grep -Eq "^CapEff:[[:space:]]+0+$" /proc/self/status
		grep -Eq "^NoNewPrivs:[[:space:]]+1$" /proc/self/status
		mkdir -p /work/repo "$HOME"
		tar --extract --file=- --directory=/work/repo
		cd /work/repo
		exec scripts/in-container/build-kselftest-flavor.sh \
			/input/flavor /output "$@"
	' sh "$flavor" "$llvm_major" "$profile_relative" "$kind" >"$log" 2>&1; then
	status=PASS
else
	rc=$?
	tail -n 160 "$log" >&2 || true
fi

mkdir -p "$stage/output/evidence"
cp "$log" "$stage/output/evidence/kselftest-orchestration.log"
if [ "$status" = FAIL ]; then
	cat >"$stage/output/evidence/result.env" <<EOF
status=FAIL
flavor=${flavor}
profile_kind=${kind}
publishable=false
EOF
fi
(
	cd "$stage/output/evidence"
	find . -type f ! -name evidence.sha256 -print0 |
		sort -z | xargs -0 -r sha256sum
) >"$stage/output/evidence/evidence.sha256"

output_root="${DKC_ROOT}/out/kselftest/${kind}/${flavor}"
output="${output_root}/${DKC_RUN_ID}"
mkdir -p "$output_root"
[ ! -e "$output" ] || dkc::die "refusing to replace existing kselftest output: $output"
mv "$stage/output" "$output"
if [ "$status" = PASS ]; then
	if [ "$update_latest" = 1 ]; then
		ln -sfn "$DKC_RUN_ID" "$output_root/latest"
	fi
	dkc::ok "${flavor} ${kind} kselftest bundle complete: ${output}"
else
	if [ "$update_latest" = 1 ]; then
		ln -sfn "$DKC_RUN_ID" "$output_root/latest-failed"
	fi
	dkc::die "${flavor} ${kind} kselftest build failed; evidence: ${output}"
fi

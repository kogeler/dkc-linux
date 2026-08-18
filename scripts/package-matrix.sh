#!/usr/bin/env bash
# Reconcile and install-test the automatic release flavor exports.

# shellcheck source=scripts/lib/common.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"

dkc::refuse_root
dkc::require_cmd podman tar tee xz
dkc::install_cleanup_trap

[ "$#" -eq 5 ] || dkc::die \
	"usage: package-matrix.sh <toolbox-image> <base-image> <llvm-major> <v2-result> <v3-result>"
toolbox="$1"
base="$2"
llvm_major="$3"
shift 3
roots=("$@")
[[ "$llvm_major" =~ ^[0-9]+$ ]] || dkc::die "invalid LLVM major"
for index in 0 1; do
	roots[index]="$(realpath "${roots[index]}")"
	test -d "${roots[index]}/artifacts" || dkc::die "matrix result lacks artifacts: ${roots[index]}"
	test -d "${roots[index]}/evidence" || dkc::die "matrix result lacks evidence: ${roots[index]}"
done

rootless="$(podman info --format '{{.Host.Security.Rootless}}' 2>/dev/null || echo false)"
[ "$rootless" = true ] || dkc::die "rootless podman is required"
podman image exists "$toolbox" || dkc::die "toolbox image is missing; run: make image"
podman image exists "$base" || dkc::die "pinned base image is missing; run: make image"

stage="${DKC_RUN_DIR}/package-matrix"
mkdir -p "$stage/repository" "$stage/evidence"
dkc::register_resource path "$stage"

export_matrix_failure() {
	local rc="$1" output_root output path
	set +e
	trap - ERR
	output_root="$DKC_ROOT/out/package-matrix"
	output="$output_root/$DKC_RUN_ID"
	if ! mkdir -p "$output_root"; then
		dkc::warn "could not create package-matrix failure output root"
		exit "$rc"
	fi
	if [ -e "$output" ]; then
		dkc::warn "refusing to replace existing failure output: $output"
		exit "$rc"
	fi
	if ! mkdir "$output"; then
		dkc::warn "could not create package-matrix failure output"
		exit "$rc"
	fi
	for path in evidence client-image client-headers flat-repository; do
		[ -e "$stage/$path" ] || continue
		if ! cp -a "$stage/$path" "$output/"; then
			dkc::warn "could not copy package-matrix failure ${path}"
			exit "$rc"
		fi
	done
	if ! mkdir -p "$output/evidence"; then
		dkc::warn "could not create package-matrix failure evidence directory"
		exit "$rc"
	fi
	if ! cat >"$output/evidence/result.env" <<EOF; then
status=FAIL
package_matrix=FAIL
publishable=false
scope=current-package-matrix
exit_code=${rc}
EOF
		dkc::warn "could not write package-matrix failure result"
		exit "$rc"
	fi
	if ! "$DKC_ROOT/scripts/package-matrix-manifest.sh" write "$output"; then
		dkc::warn "could not write package-matrix failure manifest"
		exit "$rc"
	fi
	if ! ln -sfn "$DKC_RUN_ID" "$output_root/latest-failed"; then
		dkc::warn "could not update package-matrix failure selector"
		exit "$rc"
	fi
	dkc::warn "retained package-matrix failure evidence: $output"
	exit "$rc"
}

matrix_on_error() {
	local rc=$?
	export_matrix_failure "$rc"
}
trap matrix_on_error ERR

audit_name="dkc-package-matrix-audit-${DKC_RUN_ID}"
dkc::register_resource container "$audit_name"
dkc::info "package matrix: reconcile exact package graph and payload paths"
if dkc::archive_worktree |
	podman run --rm --interactive --network=none --read-only \
		--read-only-tmpfs=false --userns=keep-id \
		--name "$audit_name" \
		--label "${DKC_LABEL_NS}=${DKC_RUN_ID}" \
		--label "${DKC_LABEL_OWNER}=${DKC_OWNER_ID}" \
		--cap-drop=ALL --security-opt=no-new-privileges \
		--pids-limit=4096 \
		--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=256m,mode=1777 \
		--tmpfs=/work:rw,exec,nosuid,nodev,size=512m,mode=1777 \
		--volume "${roots[0]}:/input/v2:ro" \
		--volume "${roots[1]}:/input/v3:ro" \
		--volume "$stage:/matrix:rw" \
		--workdir /work \
		--env HOME=/work/home \
		--env PYTHONDONTWRITEBYTECODE=1 \
		"$toolbox" sh -ceu '
		test "$(id -u)" -ne 0
		grep -Eq "^CapEff:[[:space:]]+0+$" /proc/self/status
		grep -Eq "^NoNewPrivs:[[:space:]]+1$" /proc/self/status
		test ! -e /work/repo
		mkdir -p /work/repo "$HOME"
		tar --extract --file=- --directory=/work/repo
		cd /work/repo
		exec python3 scripts/in-container/audit-package-matrix.py "$@"
	' sh /input/v2 /input/v3 \
		/matrix/repository /matrix/evidence/package-matrix.json \
		2>&1 | tee "$stage/evidence/package-audit.log"; then
	:
else
	audit_rc=$?
	dkc::warn "package reconciliation failed with rc=${audit_rc}"
	export_matrix_failure "$audit_rc"
fi

# shellcheck disable=SC2054  # comma-separated tmpfs options are one argument
client_flags=(
	--rm
	--label "${DKC_LABEL_NS}=${DKC_RUN_ID}"
	--label "${DKC_LABEL_OWNER}=${DKC_OWNER_ID}"
	--security-opt=no-new-privileges
	--pids-limit=32768
	--tmpfs=/tmp:rw,exec,nosuid,nodev,size=4g,mode=1777
	--volume "$stage:/matrix:ro"
	--env DEBIAN_FRONTEND=noninteractive
	--env LC_ALL=C.UTF-8
	--env LANG=C.UTF-8
	--env TZ=UTC
)
if [ -n "${DKC_PODMAN_NETWORK:-}" ]; then
	client_flags+=(--network="$DKC_PODMAN_NETWORK")
fi

for mode in image headers; do
	evidence="$stage/client-${mode}"
	mkdir "$evidence"
	name="dkc-package-${mode}-client-${DKC_RUN_ID}"
	dkc::register_resource container "$name"
	dkc::info "package matrix: clean Trixie ${mode} client"
	dkc::archive_worktree |
		podman run --interactive "${client_flags[@]}" \
			--name "$name" \
			--volume "$evidence:/evidence:rw" \
			"$base" sh -ceu '
			test ! -e /repo
			mkdir /repo
			tar --extract --file=- --directory=/repo
			exec /repo/scripts/in-container/test-package-client.sh "$@"
		' sh "$mode" "$llvm_major"
	if ! grep -qx 'status=PASS' "$evidence/result-${mode}.env"; then
		dkc::err "${mode} package client did not pass"
		export_matrix_failure 1
	fi
done

mv "$stage/repository" "$stage/flat-repository"

for log in "$stage"/client-*/*.log; do
	[ -f "$log" ] || continue
	sha256sum "$log" >"${log}.sha256"
	xz --threads=1 --check=sha256 -1 "$log"
done

cat >"$stage/evidence/result.env" <<EOF
status=PASS
package_matrix=PASS
image_client=PASS
headers_dkms_client=PASS
source_packages=PASS
upgrade_between_dkc_revisions=NOT_RUN
publishable=false
scope=current-package-matrix
EOF
"$DKC_ROOT/scripts/package-matrix-manifest.sh" write "$stage"
"$DKC_ROOT/scripts/package-matrix-manifest.sh" verify-full "$stage"

output_root="$DKC_ROOT/out/package-matrix"
output="$output_root/$DKC_RUN_ID"
mkdir -p "$output_root"
test ! -e "$output" || dkc::die "refusing to replace existing output $output"
mkdir "$output"
mv "$stage/evidence" "$output/"
mv "$stage/client-image" "$output/"
mv "$stage/client-headers" "$output/"
mv "$stage/flat-repository" "$output/"
ln -sfn "$DKC_RUN_ID" "$output_root/latest"

# The success output is durable now; restore the common diagnostic trap for
# any future statement added below this point.
trap dkc::_on_err ERR

dkc::ok "current package matrix complete: $output"

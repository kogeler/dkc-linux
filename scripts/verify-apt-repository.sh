#!/usr/bin/env bash
# Merge a bounded signature overlay and verify the complete repository without secrets.

# shellcheck source=scripts/lib/common.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"

dkc::refuse_root
dkc::require_cmd podman tar realpath xz
dkc::install_cleanup_trap

[ "$#" -eq 5 ] || dkc::die \
	"usage: verify-apt-repository.sh <toolbox> <apt-client-image> <unsigned-result> <signature-result> <keys-dir>"
toolbox="$1"
client_image="$2"
unsigned="$(realpath "$3")"
signature="$(realpath "$4")"
keys_dir="$(realpath "$5")"
stage="$DKC_RUN_DIR/apt-repository"
mkdir -p "$stage/output/evidence" "$stage/client"
dkc::register_resource path "$stage"
output_root="$DKC_ROOT/out/apt-repository"
output="$output_root/$DKC_RUN_ID"
failure_phase=input

export_verify_failure() {
	local rc="$1" path
	set +e
	trap - ERR
	mkdir -p "$output_root"
	if [ -e "$output" ] || ! mkdir "$output"; then
		dkc::warn "could not create verified-repository failure output: $output"
		exit "$rc"
	fi
	for path in "$stage/output/evidence" "$stage/client"; do
		[ -e "$path" ] || continue
		cp -a "$path" "$output/" || {
			dkc::warn "could not retain repository verification evidence"
			exit "$rc"
		}
	done
	mkdir -p "$output/evidence"
	cat >"$output/evidence/result.env" <<EOF
status=FAIL
repository_verification=FAIL
failure_phase=${failure_phase}
publishable=false
exit_code=${rc}
EOF
	(
		cd "$output"
		find . -type f ! -path './evidence/evidence.sha256' -print0 |
			sort -z | xargs -0 -r sha256sum
	) >"$output/evidence/evidence.sha256"
	ln -sfn "$DKC_RUN_ID" "$output_root/latest-failed"
	dkc::warn "retained repository verification failure evidence: $output"
	exit "$rc"
}

verify_on_error() {
	local rc=$?
	export_verify_failure "$rc"
}
trap verify_on_error ERR

for result in "$unsigned" "$signature"; do
	[ -d "$result/evidence" ] || {
		dkc::warn "APT handoff lacks evidence: $result"
		false
	}
	(
		cd "$result"
		sha256sum --check evidence/evidence.sha256 >/dev/null
	)
	grep -qx 'status=PASS' "$result/evidence/result.env"
done
if [ ! -d "$unsigned/repository" ] || [ ! -f "$unsigned/handoff/signing-request.json" ]; then
	dkc::warn "unsigned APT handoff is incomplete"
	false
fi
[ -d "$signature/overlay" ] || {
	dkc::warn "APT signature handoff is incomplete"
	false
}
podman image exists "$toolbox" || {
	dkc::warn "toolbox image is missing; run: make image"
	false
}
podman image exists "$client_image" || {
	dkc::warn "APT client image is missing; run: make apt-client-image"
	false
}
[ "$(podman info --format '{{.Host.Security.Rootless}}' 2>/dev/null)" = true ] || {
	dkc::warn "rootless podman is required"
	false
}

failure_phase=merge
merge_name="dkc-apt-merge-${DKC_RUN_ID}"
dkc::register_resource container "$merge_name"
dkc::info "verifying and merging the bounded APT signature overlay"
if dkc::archive_worktree |
	podman run --rm --interactive --network=none --read-only \
		--read-only-tmpfs=false --userns=keep-id --name "$merge_name" \
		--label "${DKC_LABEL_NS}=${DKC_RUN_ID}" \
		--label "${DKC_LABEL_OWNER}=${DKC_OWNER_ID}" \
		--cap-drop=ALL --security-opt=no-new-privileges \
		--no-hosts --ipc=private --pid=private --uts=private --cgroupns=private \
		--pids-limit=1024 --umask=077 --log-driver=none \
		--env HOME=/work/home --env LC_ALL=C.UTF-8 --env LANG=C.UTF-8 --env TZ=UTC \
		--env PYTHONDONTWRITEBYTECODE=1 \
		--tmpfs=/tmp:rw,noexec,nosuid,nodev,size=128m,mode=1777 \
		--tmpfs=/work:rw,exec,nosuid,nodev,size=512m,mode=1777 \
		--volume "${unsigned}:/input/unsigned:ro" \
		--volume "${signature}:/input/signature:ro" \
		--volume "${keys_dir}:/input/keys:ro" \
		--volume "${stage}/output:/output:rw" \
		--workdir /work "$toolbox" sh -ceu '
		test "$(id -u)" -ne 0
		grep -Eq "^CapEff:[[:space:]]+0+$" /proc/self/status
		grep -Eq "^NoNewPrivs:[[:space:]]+1$" /proc/self/status
		mkdir -p /work/repo "$HOME"
		tar --extract --file=- --directory=/work/repo
		cd /work/repo
		exec python3 scripts/in-container/build-signed-repository.py merge \
			--unsigned /input/unsigned/repository \
			--request /input/unsigned/handoff/signing-request.json \
			--overlay /input/signature/overlay \
			--public-keyring /input/keys/dkc-archive-keyring.gpg \
			--primary-fingerprint /input/keys/archive-primary.fingerprint \
			--signing-subkeys /input/keys/archive-signing-subkeys.fingerprints \
			--output /output/repository
	' sh >"$stage/output/evidence/merge.json" \
		2>"$stage/output/evidence/merge.log"; then
	:
else
	rc=$?
	tail -n 120 "$stage/output/evidence/merge.log" >&2 || true
	dkc::warn "APT signature handoff verification failed with rc=${rc}"
	export_verify_failure "$rc"
fi

client_name="dkc-apt-client-${DKC_RUN_ID}"
client_work="dkc-apt-client-work-${DKC_RUN_ID}"
podman volume create \
	--label "${DKC_LABEL_NS}=${DKC_RUN_ID}" \
	--label "${DKC_LABEL_OWNER}=${DKC_OWNER_ID}" \
	"$client_work" >/dev/null
dkc::register_resource volume "$client_work"
dkc::register_resource container "$client_name"
dkc::info "running the complete clean APT binary/source/signature client"
failure_phase=client
if dkc::archive_worktree |
	podman run --rm --interactive --network=none \
		--name "$client_name" \
		--label "${DKC_LABEL_NS}=${DKC_RUN_ID}" \
		--label "${DKC_LABEL_OWNER}=${DKC_OWNER_ID}" \
		--security-opt=no-new-privileges --pids-limit=4096 \
		--tmpfs=/tmp:rw,exec,nosuid,nodev,size=1g,mode=1777 \
		--volume "$client_work:/work:rw" \
		--volume "$stage/output/repository:/repository:ro" \
		--volume "$stage/client:/evidence:rw" \
		--env DKC_CLIENT_WORK=/work \
		--env DEBIAN_FRONTEND=noninteractive \
		--env LC_ALL=C.UTF-8 --env LANG=C.UTF-8 --env TZ=UTC \
		"$client_image" sh -ceu '
		test ! -e /repo
		mkdir /repo
		tar --extract --file=- --directory=/repo
		exec /repo/scripts/in-container/test-signed-repository.sh /repository /evidence
	'; then
	:
else
	rc=$?
	dkc::warn "complete APT client failed with rc=${rc}"
	export_verify_failure "$rc"
fi
failure_phase=finalize
grep -qx 'status=PASS' "$stage/client/result.env"
for log in "$stage/client"/*.log; do
	[ -f "$log" ] || continue
	sha256sum "$log" >"${log}.sha256"
	xz --threads=1 --check=sha256 -1 "$log"
done
mv "$stage/client" "$stage/output/client"
cat >"$stage/output/evidence/result.env" <<EOF
status=PASS
repository_assembly=PASS
repository_signing=PASS
signature_handoff=PASS
signed_apt_client=PASS
source_packages=PASS
by_hash=PASS
publishable=false
EOF
(
	cd "$stage/output"
	find . -type f ! -path './evidence/evidence.sha256' -print0 |
		sort -z | xargs -0 -r sha256sum
) >"$stage/output/evidence/evidence.sha256"

mkdir -p "$output_root"
[ ! -e "$output" ] || dkc::die "refusing to replace verified APT output: $output"
trap - ERR
mv "$stage/output" "$output"
ln -sfn "$DKC_RUN_ID" "$output_root/latest"
dkc::ok "verified signed APT repository complete: $output"

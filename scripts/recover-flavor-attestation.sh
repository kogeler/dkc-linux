#!/usr/bin/env bash
# Recover an accepted export after compilation completed but an attestation gate failed.

# shellcheck source=scripts/lib/common.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"
# shellcheck source=scripts/lib/podman-image.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib/podman-image.sh"

dkc::refuse_root
dkc::require_cmd podman realpath tar tee
dkc::install_cleanup_trap

[ "$#" -eq 5 ] || dkc::die \
	"usage: recover-flavor-attestation.sh <image> <llvm-major> <v2|v3|v4> <failed-result> <update-latest>"
image="$1"
llvm_major="$2"
flavor="$3"
input="$(realpath "$4")"
update_latest="$5"
[[ "$llvm_major" =~ ^[0-9]+$ ]] || dkc::die "invalid LLVM major"
[[ "$flavor" =~ ^v[234]$ ]] || dkc::die "invalid flavor"
[[ "$update_latest" =~ ^[01]$ ]] || dkc::die "UPDATE_LATEST must be 0 or 1"
if [ ! -d "$input/artifacts" ] || [ ! -d "$input/evidence" ] || [ ! -d "$input/source" ]; then
	dkc::die "failed result lacks artifacts, evidence, or source: $input"
fi
grep -qx 'status=FAIL' "$input/evidence/result.env" ||
	dkc::die "recovery input is not a failed result"
grep -qx 'failure_phase=offline-build' "$input/evidence/result.env" ||
	dkc::die "only an offline-build attestation failure can be recovered"
grep -qx "flavor=${flavor}" "$input/evidence/result.env" ||
	dkc::die "recovery input has a different flavor"

[ "$(podman info --format '{{.Host.Security.Rootless}}' 2>/dev/null)" = true ] ||
	dkc::die "rootless podman is required"
expected_image_id="$(cat "$input/evidence/build-image.id")"
[[ "$expected_image_id" =~ ^sha256:[0-9a-f]{64}$ ]] ||
	dkc::die "failed result has an invalid build image ID"
actual_image_id=""
if podman image exists "$image"; then
	raw_image_id="$(podman image inspect "$image" --format '{{.Id}}')"
	actual_image_id="$(dkc::canonical_podman_image_id "$raw_image_id")" ||
		dkc::die "selected build image has no valid config digest"
fi
if [ "$actual_image_id" = "$expected_image_id" ]; then
	:
elif podman image exists "$expected_image_id"; then
	dkc::info "using the retained build image config digest from the failed result"
	image="$expected_image_id"
else
	dkc::die "original build image is unavailable; retain it or pull its recorded registry digest"
fi

stage="${DKC_RUN_DIR}/recover-attestation-${flavor}"
mkdir -p "$stage/output"
dkc::register_resource path "$stage"
volume="dkc-recover-attestation-${flavor}-${DKC_RUN_ID}"
podman volume create \
	--label "${DKC_LABEL_NS}=${DKC_RUN_ID}" \
	--label "${DKC_LABEL_OWNER}=${DKC_OWNER_ID}" \
	"$volume" >/dev/null
dkc::register_resource volume "$volume"
container="dkc-recover-attestation-${flavor}-${DKC_RUN_ID}"
dkc::register_resource container "$container"
log="$stage/recovery.log"

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
		--volume "${input}:/input/result:ro" \
		--workdir /work "$image" sh -ceu '
		test "$(id -u)" -ne 0
		grep -Eq "^CapEff:[[:space:]]+0+$" /proc/self/status
		grep -Eq "^NoNewPrivs:[[:space:]]+1$" /proc/self/status
		mkdir -p /work/repo "$HOME"
		tar --extract --file=- --directory=/work/repo
		(
			cd /input/result/evidence
			sha256sum --check evidence.sha256 >/dev/null
		)
		(
			cd /input/result/artifacts
			sha256sum --check /input/result/evidence/artifacts.sha256 >/dev/null
			sha256sum --check /input/result/evidence/failure-artifacts.sha256 >/dev/null
		)
		python3 - /input/result/evidence/post-build-gates.env <<"PY"
import pathlib
import sys

values = {}
for line in pathlib.Path(sys.argv[1]).read_text(encoding="utf-8").splitlines():
    key, separator, value = line.partition("=")
    if not separator or key in values:
        raise SystemExit("post-build gate result is malformed")
    values[key] = value
if set(values) != {
    "package_attestation_rc",
    "kbuild_audit_rc",
    "simd_audit_rc",
    "lintian_rc",
}:
    raise SystemExit("post-build gate set is incomplete")
if values["lintian_rc"] != "0":
    raise SystemExit("lintian did not pass and cannot be replayed")
for key in ("package_attestation_rc", "kbuild_audit_rc", "simd_audit_rc"):
    if not values[key].isdigit():
        raise SystemExit(f"invalid gate return code: {key}")
PY
		mkdir -p /work/reattested/evidence
		/work/repo/scripts/in-container/reattest-flavor.sh \
			/input/result /work/reattested "$1" "$2" observations 1>&2

		mkdir -p "/work/results/$2" /work/inputs /work/source-package
		cp -a /input/result/artifacts "/work/results/$2/"
		cp -a /input/result/evidence "/work/results/$2/"
		result="/work/results/$2"
		for name in attestation.json package-fields.json kbuild-commands.tsv.xz \
			kbuild-command-audit.json kernel-simd-audit.json; do
			cp "/work/reattested/evidence/$name" "$result/evidence/$name"
		done
		cat >"$result/evidence/post-build-gates.env" <<EOF
package_attestation_rc=0
kbuild_audit_rc=0
simd_audit_rc=0
lintian_rc=0
EOF
		rm -f -- "$result/evidence/evidence.sha256" \
			"$result/evidence/failure-artifacts.sha256" \
			"$result/evidence/result.env"

		find /input/result/source -mindepth 1 -maxdepth 1 -type f -print0 |
			sort -z | xargs -0 cp -t /work/source-package --
		for name in source-inventory.json toolchain.env build-image-packages.tsv \
			apt-indexes.sha256 build-image-debs.tsv staging-apt-indexes.sha256 \
			repository-inputs.sha256 publication-identity.json policy-config-v2.json \
			policy-config-v3.json policy-config-v4.json; do
			cp "/input/result/evidence/$name" "/work/inputs/$name"
		done
		cp /input/result/evidence/build-image.id /work/inputs/build-image.id
		if test -f /input/result/evidence/build-image-provenance.env; then
			cp /input/result/evidence/build-image-provenance.env /work/inputs/
		fi
		exec /work/repo/scripts/in-container/finalize-one-build.sh "$2"
	' sh "$llvm_major" "$flavor" 2> >(tee "$log" >&2) |
	tar --extract --file=- --directory="$stage/output" --no-same-owner; then
	:
else
	rc=$?
	dkc::die "flavor attestation recovery failed with rc=${rc}; log: ${log}"
fi

grep -qx 'status=PASS' "$stage/output/evidence/result.env" ||
	dkc::die "recovered attestation export did not pass final acceptance"
cp "$log" "$stage/output/evidence/recovery.log"
cat >"$stage/output/evidence/recovery.env" <<EOF
status=PASS
flavor=${flavor}
source_result=${input}
source_failure_phase=offline-build
compilation_reused=true
complete_simd_observations_reused=true
package_attestation_replayed=true
kbuild_attestation_replayed=true
simd_policy_replayed=true
EOF
(
	cd "$stage/output/evidence"
	find . -type f ! -name evidence.sha256 -print0 |
		sort -z | xargs -0 sha256sum
) >"$stage/output/evidence/evidence.sha256"

output_root="${DKC_ROOT}/out/flavors/${flavor}"
output="${output_root}/${DKC_RUN_ID}"
mkdir -p "$output_root"
test ! -e "$output" || dkc::die "refusing to replace existing output: $output"
mv "$stage/output" "$output"
if [ "$update_latest" = 1 ]; then
	ln -sfn "$DKC_RUN_ID" "$output_root/latest"
fi
dkc::ok "${flavor} attestation recovered without recompilation: ${output}"

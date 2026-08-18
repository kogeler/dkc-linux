#!/usr/bin/env bash
# Orchestrate one flavor as a networked stage and an offline builder.

# shellcheck source=scripts/lib/common.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"
# shellcheck source=scripts/lib/podman-image.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib/podman-image.sh"

dkc::refuse_root
dkc::require_cmd podman tar tee
dkc::install_cleanup_trap

[ "$#" -eq 20 ] || dkc::die "build-one.sh received the wrong argument count"

image="$1" llvm_major="$2" jobs="$3" flavor="$4"
dsc_url="$5" dsc_name="$6" dsc_sha="$7" dsc_size="$8"
orig_url="$9" orig_name="${10}" orig_sha="${11}" orig_size="${12}"
debian_url="${13}" debian_name="${14}" debian_sha="${15}" debian_size="${16}"
source_version="${17}"
dkc_revision="${18}"
lto_mode="${19}"
update_latest="${20}"

[[ "$llvm_major" =~ ^[0-9]+$ && "$jobs" =~ ^[1-9][0-9]*$ && "$dkc_revision" =~ ^[1-9][0-9]*$ ]] ||
	dkc::die "invalid LLVM major, job count, or DKC revision"
[[ "$source_version" =~ ^[0-9A-Za-z.+:~_-]+$ ]] || dkc::die "unsafe source version"
[[ "$flavor" =~ ^v[234]$ ]] || dkc::die "flavor must be v2, v3, or v4"
case "$lto_mode" in
none | thin | full) ;;
*) dkc::die "kernel LTO mode must be none, thin, or full" ;;
esac
[[ "$update_latest" =~ ^[01]$ ]] || dkc::die "UPDATE_LATEST must be 0 or 1"

rootless="$(podman info --format '{{.Host.Security.Rootless}}' 2>/dev/null || echo false)"
[ "$rootless" = true ] || dkc::die "rootless podman is required"
podman image exists "$image" || dkc::die "build image is missing; run: make build-image"
raw_image_id="$(podman image inspect "$image" --format '{{.Id}}')"
image_id="$(dkc::canonical_podman_image_id "$raw_image_id")" ||
	dkc::die "build image has no valid config digest"
image_size="$(podman image inspect "$image" --format '{{.Size}}')"
image_role="$(podman image inspect "$image" --format \
	'{{ index .Labels "io.github.kogeler.dkc.image-role" }}')"
image_bundle_input="$(podman image inspect "$image" --format \
	'{{ index .Labels "io.github.kogeler.dkc.bundle-input-sha256" }}')"
image_bundle_generation="$(podman image inspect "$image" --format \
	'{{ index .Labels "io.github.kogeler.dkc.bundle-generation" }}')"
image_base="$(podman image inspect "$image" --format \
	'{{ index .Labels "io.github.kogeler.dkc.base-image" }}')"
image_llvm="$(podman image inspect "$image" --format \
	'{{ index .Labels "io.github.kogeler.dkc.llvm-major" }}')"
[[ "$image_size" =~ ^[1-9][0-9]*$ ]] || dkc::die "build image reported an invalid size"
[ "$image_role" = kernel-build ] || dkc::die "build image has the wrong role; run: make build-image"
[[ "$image_bundle_input" =~ ^[0-9a-f]{64}$ ]] ||
	dkc::die "build image has no valid bundle input fingerprint; run: make build-image"
[[ "$image_bundle_generation" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$ ]] ||
	dkc::die "build image has no valid bundle generation; run: make build-image"
[ "$image_base" = "$(cat "$DKC_ROOT/config/base-image.lock")" ] ||
	dkc::die "build image has the wrong pinned base; run: make build-image"
[ "$image_llvm" = "$llvm_major" ] ||
	dkc::die "build image has the wrong LLVM major; run: make build-image"
if [[ "$image" =~ ^ghcr\.io/kogeler/dkc-kernel-build@(sha256:[0-9a-f]{64})$ ]]; then
	image_provider=registry
	image_manifest_digest="${BASH_REMATCH[1]}"
else
	image_provider=local
	image_manifest_digest=NOT_APPLICABLE
fi

image_provenance="${DKC_RUN_DIR}/build-image-provenance.env"
cat >"$image_provenance" <<EOF
provider=${image_provider}
registry_manifest_digest=${image_manifest_digest}
config_digest=${image_id}
bundle_input_sha256=${image_bundle_input}
bundle_generation=${image_bundle_generation}
EOF
dkc::register_resource path "$image_provenance"

volume="dkc-flavor-${flavor}-${DKC_RUN_ID}"
podman volume create \
	--label "${DKC_LABEL_NS}=${DKC_RUN_ID}" \
	--label "${DKC_LABEL_OWNER}=${DKC_OWNER_ID}" \
	"$volume" >/dev/null
dkc::register_resource volume "$volume"

# shellcheck disable=SC2054  # comma-separated mount options are one argument
base_flags=(
	--rm
	--read-only
	--read-only-tmpfs=false
	--userns=keep-id
	--label "${DKC_LABEL_NS}=${DKC_RUN_ID}"
	--label "${DKC_LABEL_OWNER}=${DKC_OWNER_ID}"
	--cap-drop=ALL
	--security-opt=no-new-privileges
	--no-hosts
	--ipc=private
	--pid=private
	--uts=private
	--cgroupns=private
	--pids-limit=32768
	--umask=077
	--log-driver=none
	--env HOME=/work/home
	--env LC_ALL=C.UTF-8
	--env LANG=C.UTF-8
	--env TZ=UTC
	--env PYTHONDONTWRITEBYTECODE=1
	--tmpfs=/tmp:rw,exec,nosuid,nodev,size=4g,mode=1777
)

# shellcheck disable=SC2016  # evaluated by the shell inside the container
confined='test "$(id -u)" -ne 0
grep -Eq "^CapEff:[[:space:]]+0+$" /proc/self/status
grep -Eq "^NoNewPrivs:[[:space:]]+1$" /proc/self/status
mkdir -p "$HOME" && chmod 700 "$HOME"'

export_failed_evidence() {
	local failure_phase="$1" failed_stage failure_name output_root output
	failed_stage="${DKC_RUN_DIR}/flavor-${flavor}-failure"
	mkdir -p "$failed_stage" || return 1
	dkc::register_resource path "$failed_stage"
	failure_name="dkc-flavor-${flavor}-failure-export-${DKC_RUN_ID}"
	dkc::register_resource container "$failure_name"

	podman run "${base_flags[@]}" --volume "${volume}:/work:rw" --network=none --name "$failure_name" "$image" \
		sh -ceu "$confined
			mkdir -p /work/results/$flavor/evidence
			for input in source-inventory.json toolchain.env build-image-packages.tsv \
				build-image-debs.tsv apt-indexes.sha256 staging-apt-indexes.sha256 \
				repository-inputs.sha256 publication-identity.json \
				policy-config-v2.json policy-config-v3.json policy-config-v4.json; do
				test ! -f /work/inputs/\$input || cp /work/inputs/\$input \
					/work/results/$flavor/evidence/\$input
			done
			if test -f /work/results/$flavor/evidence/build.log; then
				cd /work/results/$flavor/evidence
				sha256sum build.log >build.log.sha256
				if ! xz --threads=1 --check=sha256 -1 build.log; then
					rm -f -- build.log.xz
					echo 'warning: failure build log compression failed; exporting the original' >&2
				fi
			fi
			if test -d /work/source-package; then
				mkdir -p /work/results/$flavor/source
				find /work/source-package -mindepth 1 -maxdepth 1 -type f -print0 |
					sort -z | xargs -0 -r cp -t /work/results/$flavor/source --
			fi
			cd /work/results/$flavor
			if test -d artifacts && test -d source; then
				tar --create --file=- evidence artifacts source
			elif test -d artifacts; then
				tar --create --file=- evidence artifacts
			else
				tar --create --file=- evidence
			fi
		" | tar --extract --file=- --directory="$failed_stage" --no-same-owner || return 1

	cat >"$failed_stage/evidence/result.env" <<EOF || return 1
status=FAIL
networked_phase=source-staging-only
offline_builds=1
independent_rebuild=NOT_RUN
publishable=false
scope=flavor-${flavor}-development
flavor=${flavor}
lto_mode=${lto_mode}
failure_phase=${failure_phase}
EOF
	printf '%s\n' "$image_id" >"$failed_stage/evidence/build-image.id" || return 1
	cp "$image_provenance" "$failed_stage/evidence/build-image-provenance.env" || return 1
	if [ -f "$stage_log" ]; then
		cp "$stage_log" "$failed_stage/evidence/source-staging.log" || return 1
	fi
	if [ -f "${controller_log:-}" ]; then
		cp "$controller_log" "$failed_stage/evidence/build-controller.log" || return 1
	fi
	if [ -d "$failed_stage/artifacts" ]; then
		(
			cd "$failed_stage/artifacts"
			find . -type f -print0 | sort -z | xargs -0 sha256sum
		) >"$failed_stage/evidence/failure-artifacts.sha256" || return 1
	fi
	if [ -f "$failed_stage/evidence/capacity.env" ]; then
		printf 'build_image_bytes=%s\n' "$image_size" \
			>>"$failed_stage/evidence/capacity.env" || return 1
	fi
	(
		cd "$failed_stage/evidence"
		find . -type f ! -name evidence.sha256 -print0 |
			sort -z | xargs -0 sha256sum
	) >"$failed_stage/evidence/evidence.sha256" || return 1
	output_root="$DKC_ROOT/out/flavors/${flavor}"
	output="$output_root/${DKC_RUN_ID}"
	mkdir -p "$output_root" || return 1
	test ! -e "$output" || return 1
	mv "$failed_stage" "$output" || return 1
	if [ "$update_latest" = 1 ]; then
		ln -sfn "$DKC_RUN_ID" "$output_root/latest-failed" || return 1
	fi
	dkc::warn "retained bounded ${flavor} failure evidence: $output"
}

stage_name="dkc-flavor-${flavor}-stage-${DKC_RUN_ID}"
dkc::register_resource container "$stage_name"
stage_log="${DKC_RUN_DIR}/flavor-${flavor}-staging.log"
: >"$stage_log"
dkc::register_resource path "$stage_log"
stage_flags=(
	"${base_flags[@]}"
	--volume "${volume}:/work:rw,U"
	--volume "${image_provenance}:/input/build-image-provenance.env:ro"
	--name "$stage_name"
)
if [ -n "${DKC_PODMAN_NETWORK:-}" ]; then
	stage_flags+=(--network="$DKC_PODMAN_NETWORK")
fi

dkc::info "${flavor} staging: verify the shared source inventory and complete .deb lock (network enabled)"
if dkc::archive_worktree |
	podman run --interactive "${stage_flags[@]}" "$image" sh -ceu "$confined
		test ! -e /work/repo
		mkdir -p /work/repo
		tar --extract --file=- --directory=/work/repo
		printf '%s\n' '$image_id' >/work/inputs-image-id.pending
		cp /input/build-image-provenance.env /work/inputs-image-provenance.env.pending
		exec /work/repo/scripts/in-container/stage-one-build.sh \"\$@\"
	" sh \
		"$dsc_url" "$dsc_name" "$dsc_sha" "$dsc_size" \
		"$orig_url" "$orig_name" "$orig_sha" "$orig_size" \
		"$debian_url" "$debian_name" "$debian_sha" "$debian_size" \
		"$source_version" "$llvm_major" 2>&1 | tee "$stage_log"; then
	:
else
	stage_rc=$?
	dkc::warn "${flavor} source staging failed with rc=${stage_rc}"
	export_failed_evidence source-staging || dkc::warn "${flavor} staging evidence could not be exported"
	exit "$stage_rc"
fi

# Move the image identity into the verified input set only after staging has
# succeeded, so a partial stage cannot be mistaken for a complete one.
identity_name="dkc-flavor-${flavor}-identity-${DKC_RUN_ID}"
dkc::register_resource container "$identity_name"
if podman run "${base_flags[@]}" --volume "${volume}:/work:rw" --network=none --name "$identity_name" "$image" \
	sh -ceu "$confined
		mv /work/inputs-image-id.pending /work/inputs/build-image.id
		mv /work/inputs-image-provenance.env.pending /work/inputs/build-image-provenance.env
	" >/dev/null; then
	:
else
	identity_rc=$?
	dkc::warn "${flavor} identity staging failed with rc=${identity_rc}"
	export_failed_evidence identity-staging || dkc::warn "${flavor} identity evidence could not be exported"
	exit "$identity_rc"
fi

build_name="dkc-flavor-${flavor}-build-${DKC_RUN_ID}"
dkc::register_resource container "$build_name"
controller_log="${DKC_RUN_DIR}/flavor-${flavor}-controller.log"
: >"$controller_log"
dkc::register_resource path "$controller_log"
dkc::info "${flavor} build: compile, package, and attest with network disabled"
if podman run "${base_flags[@]}" --volume "${volume}:/work:rw" --network=none --name "$build_name" "$image" \
	sh -ceu "$confined
		exec /work/repo/scripts/in-container/run-one-build.sh \"\$@\"
	" sh "$flavor" "$flavor" "$source_version" "$dsc_name" "$llvm_major" "$jobs" \
	"$dkc_revision" "$lto_mode" 2>&1 | tee "$controller_log"; then
	:
else
	build_rc=$?
	dkc::warn "${flavor} build failed with rc=${build_rc}; attempting bounded evidence export"
	export_failed_evidence offline-build || dkc::warn "${flavor} failure evidence could not be exported"
	exit "$build_rc"
fi

export_stage="${DKC_RUN_DIR}/flavor-${flavor}-export"
mkdir -p "$export_stage"
dkc::register_resource path "$export_stage"
finalize_name="dkc-flavor-${flavor}-finalize-${DKC_RUN_ID}"
dkc::register_resource container "$finalize_name"
dkc::info "${flavor} final gate: export accepted artifacts and evidence"
if podman run "${base_flags[@]}" --volume "${volume}:/work:rw" --network=none --name "$finalize_name" "$image" \
	sh -ceu "$confined
		exec /work/repo/scripts/in-container/finalize-one-build.sh '$flavor'
	" | tar --extract --file=- --directory="$export_stage" --no-same-owner; then
	:
else
	finalize_rc=$?
	dkc::warn "${flavor} final export failed with rc=${finalize_rc}"
	export_failed_evidence final-export || dkc::warn "${flavor} finalizer evidence could not be exported"
	exit "$finalize_rc"
fi

if ! test -f "$export_stage/evidence/result.env" ||
	! grep -qx 'status=PASS' "$export_stage/evidence/result.env"; then
	dkc::warn "${flavor} host acceptance did not receive a passing result"
	export_failed_evidence host-acceptance || dkc::warn "${flavor} host acceptance evidence could not be exported"
	exit 1
fi
cmp "$image_provenance" "$export_stage/evidence/build-image-provenance.env" ||
	dkc::die "final export changed the build image provenance"
printf '%s\n' "$image_id" >"$export_stage/evidence/build-image.id"
printf 'build_image_bytes=%s\n' "$image_size" >>"$export_stage/evidence/capacity.env"
cp "$stage_log" "$export_stage/evidence/source-staging.log"
cp "$controller_log" "$export_stage/evidence/build-controller.log"
(
	cd "$export_stage/evidence"
	find . -type f ! -name evidence.sha256 -print0 |
		sort -z | xargs -0 sha256sum
) >"$export_stage/evidence/evidence.sha256"

output_root="$DKC_ROOT/out/flavors/${flavor}"
output="$output_root/$DKC_RUN_ID"
mkdir -p "$output_root"
test ! -e "$output" || dkc::die "refusing to replace existing output $output"
mv "$export_stage" "$output"
if [ "$update_latest" = 1 ]; then
	ln -sfn "$DKC_RUN_ID" "$output_root/latest"
fi

dkc::ok "${flavor} development build complete (LTO=${lto_mode}): $output"

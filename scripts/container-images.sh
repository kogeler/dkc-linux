#!/usr/bin/env bash
# Build, verify, publish, and resolve the three container images used by DKC.

# shellcheck source=scripts/lib/common.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"

export LC_ALL=C

readonly inventory_file="${DKC_ROOT}/config/container-images.inputs"
readonly source_repository="https://github.com/kogeler/dkc-linux"
readonly role_label="io.github.kogeler.dkc.image-role"
readonly input_label="io.github.kogeler.dkc.bundle-input-sha256"
readonly generation_label="io.github.kogeler.dkc.bundle-generation"
readonly base_label="io.github.kogeler.dkc.base-image"
readonly llvm_label="io.github.kogeler.dkc.llvm-major"

usage() {
	cat >&2 <<'EOF'
usage:
  container-images.sh fingerprint <base-image> <llvm-major>
  container-images.sh paths <content|trigger|all>
  container-images.sh build <role> <image> <base> <llvm> <input-sha> <generation> <revision>
  container-images.sh ensure <role> <digest-ref>
  container-images.sh ensure-base <base-image>
  container-images.sh push-bundle <toolbox> <build> <apt-client> <base> <llvm> <input-sha> <generation> <toolbox-latest> <build-latest> <apt-client-latest>
  container-images.sh resolve <expected-generation-or-empty> <timeout> <interval> <output> <toolbox-latest> <build-latest> <apt-client-latest>
EOF
	exit 2
}

validate_role() {
	case "$1" in
	toolbox | kernel-build | apt-client) ;;
	*) dkc::die "unknown container image role: $1" ;;
	esac
}

validate_sha256() {
	[[ "$1" =~ ^[0-9a-f]{64}$ ]] || dkc::die "$2 is not a lowercase SHA-256"
}

validate_llvm() {
	[[ "$1" =~ ^[0-9]+$ ]] || dkc::die "LLVM major must be a positive integer"
	[ "$1" -gt 0 ] || dkc::die "LLVM major must be a positive integer"
}

validate_generation() {
	[[ "$1" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$ ]] ||
		dkc::die "unsafe container bundle generation"
}

validate_revision() {
	[[ "$1" =~ ^[0-9a-f]{40}$ ]] || dkc::die "image source revision is not a full Git SHA"
}

validate_inventory() {
	[ -f "$inventory_file" ] || dkc::die "container image input inventory is missing"
	local previous="" kind path extra entry
	while read -r kind path extra; do
		[ -n "$kind" ] || continue
		[[ "$kind" == \#* ]] && continue
		[ -z "${extra:-}" ] || dkc::die "malformed container image input inventory entry"
		case "$kind" in
		content | trigger) ;;
		*) dkc::die "unknown container image input kind: $kind" ;;
		esac
		[[ "$path" =~ ^[A-Za-z0-9_./-]+$ ]] || dkc::die "unsafe container image input path"
		[[ "$path" != /* && "$path" != *".."* ]] || dkc::die "unsafe container image input path"
		[ -f "${DKC_ROOT}/${path}" ] || dkc::die "container image input is missing: $path"
		entry="${path} ${kind}"
		if [ -n "$previous" ] && [[ "$entry" < "$previous" || "$entry" == "$previous" ]]; then
			dkc::die "container image input inventory must be unique and sorted"
		fi
		previous="$entry"
	done <"$inventory_file"
}

list_paths() {
	local requested="$1" kind path extra
	case "$requested" in
	content | trigger | all) ;;
	*) usage ;;
	esac
	validate_inventory
	while read -r kind path extra; do
		[ -n "$kind" ] || continue
		[[ "$kind" == \#* ]] && continue
		if [ "$requested" = all ] || [ "$kind" = content ] || [ "$kind" = "$requested" ]; then
			printf '%s\n' "$path"
		fi
	done <"$inventory_file"
}

fingerprint() {
	local base_image="$1" llvm_major="$2" kind path extra size
	[ -n "$base_image" ] || dkc::die "base image is empty"
	validate_llvm "$llvm_major"
	validate_inventory
	{
		printf 'dkc-container-image-bundle-v1\0'
		printf 'build-argument\0BASE_IMAGE\0%s\0' "$base_image"
		printf 'build-argument\0LLVM_MAJOR\0%s\0' "$llvm_major"
		while read -r kind path extra; do
			[ -n "$kind" ] || continue
			[[ "$kind" == \#* ]] && continue
			[ "$kind" = content ] || continue
			size="$(stat -c '%s' "${DKC_ROOT}/${path}")"
			printf 'file\0%s\0%s\0' "$path" "$size"
			cat "${DKC_ROOT}/${path}"
			printf '\0'
		done <"$inventory_file"
	} | sha256sum | awk '{print $1}'
}

role_containerfile() {
	case "$1" in
	toolbox) printf '%s\n' "${DKC_ROOT}/container/Containerfile.toolbox" ;;
	kernel-build) printf '%s\n' "${DKC_ROOT}/container/Containerfile.build" ;;
	apt-client) printf '%s\n' "${DKC_ROOT}/container/Containerfile.apt-client" ;;
	esac
}

role_context() {
	case "$1" in
	toolbox | kernel-build | apt-client) printf '%s\n' "${DKC_ROOT}/container" ;;
	esac
}

role_title() {
	case "$1" in
	toolbox) printf '%s\n' "DKC toolbox" ;;
	kernel-build) printf '%s\n' "DKC kernel build environment" ;;
	apt-client) printf '%s\n' "DKC APT verification client" ;;
	esac
}

role_remote_name() {
	case "$1" in
	toolbox) printf '%s\n' dkc-toolbox ;;
	kernel-build) printf '%s\n' dkc-kernel-build ;;
	apt-client) printf '%s\n' dkc-apt-client ;;
	esac
}

inspect_format() {
	local image="$1" format="$2"
	podman image inspect "$image" --format "$format" 2>/dev/null
}

inspect_label() {
	local image="$1" key="$2"
	inspect_format "$image" "{{ index .Labels \"${key}\" }}"
}

verify_image() {
	local role="$1" image="$2" base_image="$3" llvm_major="$4"
	local expected_input="$5" expected_generation="$6"
	local actual role_value input_value generation_value base_value llvm_value source_value revision_value
	validate_role "$role"
	if ! podman image exists "$image"; then
		dkc::warn "container image is absent: $image"
		return 1
	fi
	actual="$(inspect_format "$image" '{{.Os}}/{{.Architecture}}')" || return 1
	if [ "$actual" != linux/amd64 ]; then
		dkc::warn "container image ${image} has platform ${actual}, expected linux/amd64"
		return 1
	fi
	role_value="$(inspect_label "$image" "$role_label")" || return 1
	input_value="$(inspect_label "$image" "$input_label")" || return 1
	generation_value="$(inspect_label "$image" "$generation_label")" || return 1
	base_value="$(inspect_label "$image" "$base_label")" || return 1
	llvm_value="$(inspect_label "$image" "$llvm_label")" || return 1
	source_value="$(inspect_label "$image" org.opencontainers.image.source)" || return 1
	revision_value="$(inspect_label "$image" org.opencontainers.image.revision)" || return 1
	if [ "$role_value" != "$role" ] || [ "$input_value" != "$expected_input" ] ||
		[ "$base_value" != "$base_image" ] || [ "$llvm_value" != "$llvm_major" ] ||
		[ "$source_value" != "$source_repository" ]; then
		dkc::warn "container image ${image} metadata does not match the requested bundle"
		return 1
	fi
	if [ -n "$expected_generation" ] && [ "$generation_value" != "$expected_generation" ]; then
		dkc::warn "container image ${image} generation does not match"
		return 1
	fi
	if ! [[ "$generation_value" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$ ]] ||
		! [[ "$revision_value" =~ ^[0-9a-f]{40}$ ]]; then
		dkc::warn "container image ${image} has malformed provenance metadata"
		return 1
	fi
	printf '%s\n' "$generation_value"
}

verify_published_image() {
	local role="$1" image="$2"
	local input_value generation_value base_value llvm_value
	input_value="$(inspect_label "$image" "$input_label")" || return 1
	generation_value="$(inspect_label "$image" "$generation_label")" || return 1
	base_value="$(inspect_label "$image" "$base_label")" || return 1
	llvm_value="$(inspect_label "$image" "$llvm_label")" || return 1
	if ! [[ "$input_value" =~ ^[0-9a-f]{64}$ ]] ||
		! [[ "$base_value" =~ ^[a-z0-9./:_-]+@sha256:[0-9a-f]{64}$ ]] ||
		! [[ "$llvm_value" =~ ^[1-9][0-9]*$ ]]; then
		dkc::warn "container image ${image} has malformed environment metadata"
		return 1
	fi
	verify_image "$role" "$image" "$base_value" "$llvm_value" \
		"$input_value" "$generation_value"
}

build_image() {
	local role="$1" image="$2" base_image="$3" llvm_major="$4"
	local input_sha="$5" generation="$6" revision="$7"
	local containerfile context title
	validate_role "$role"
	validate_sha256 "$input_sha" "bundle input fingerprint"
	validate_llvm "$llvm_major"
	validate_generation "$generation"
	validate_revision "$revision"
	containerfile="$(role_containerfile "$role")"
	context="$(role_context "$role")"
	title="$(role_title "$role")"
	local build_args=(
		--layers
		--build-arg "BASE_IMAGE=${base_image}"
		--label "${role_label}=${role}"
		--label "${input_label}=${input_sha}"
		--label "${generation_label}=${generation}"
		--label "${base_label}=${base_image}"
		--label "${llvm_label}=${llvm_major}"
		--label "org.opencontainers.image.source=${source_repository}"
		--label "org.opencontainers.image.revision=${revision}"
		--label "org.opencontainers.image.title=${title}"
		--file "$containerfile"
		--tag "$image"
	)
	if [ "$role" = kernel-build ]; then
		build_args+=(
			--build-arg "LLVM_MAJOR=${llvm_major}"
		)
	fi
	dkc::info "building ${role} image ${image}"
	podman build "${build_args[@]}" "$context"
	verify_image "$role" "$image" "$base_image" "$llvm_major" "$input_sha" "$generation" >/dev/null ||
		dkc::die "built ${role} image failed metadata verification"
	if [ "$role" = kernel-build ]; then
		podman run --rm --network=none "$image" cat /usr/share/dkc/toolchain.env
	fi
	dkc::ok "built and verified ${role} image"
}

validate_digest_reference() {
	local role="$1" image="$2" remote
	remote="$(role_remote_name "$role")"
	[[ "$image" =~ ^ghcr\.io/kogeler/${remote}@sha256:[0-9a-f]{64}$ ]] ||
		dkc::die "${role} registry image must be an immutable canonical GHCR digest reference"
}

ensure_image() {
	local role="$1" image="$2"
	validate_role "$role"
	validate_digest_reference "$role" "$image"
	if podman image exists "$image" &&
		verify_published_image "$role" "$image" >/dev/null; then
		dkc::ok "immutable ${role} image is already ready"
		return 0
	fi
	dkc::info "pulling immutable ${role} image ${image}"
	podman pull "$image"
	verify_published_image "$role" "$image" >/dev/null ||
		dkc::die "pulled ${role} image failed published-image verification"
	dkc::ok "immutable ${role} image is ready"
}

ensure_base() {
	local base_image="$1"
	[ -n "$base_image" ] || dkc::die "base image is empty"
	if ! podman image exists "$base_image"; then
		dkc::info "pulling digest-pinned base image"
		podman pull "$base_image"
	fi
	podman image exists "$base_image" || dkc::die "digest-pinned base image is unavailable"
}

validate_latest_reference() {
	local role="$1" image="$2" remote
	remote="$(role_remote_name "$role")"
	[ "$image" = "ghcr.io/kogeler/${remote}:latest" ] ||
		dkc::die "${role} publication target must be its canonical latest tag"
}

push_bundle() {
	local toolbox="$1" build="$2" client="$3" base_image="$4" llvm_major="$5"
	local input_sha="$6" generation="$7" toolbox_latest="$8" build_latest="$9"
	local client_latest="${10}"
	[ "${GITHUB_ACTIONS:-}" = true ] ||
		dkc::die "container image publication is restricted to GitHub Actions"
	[ "${GITHUB_REPOSITORY:-}" = kogeler/dkc-linux ] ||
		dkc::die "container image publication is restricted to the canonical repository"
	[ "${GITHUB_REF:-}" = refs/heads/main ] ||
		dkc::die "container image publication is restricted to main"
	case "${GITHUB_EVENT_NAME:-}" in
	push | schedule | workflow_dispatch) ;;
	*) dkc::die "container image publication is not allowed for this event" ;;
	esac
	validate_revision "${GITHUB_SHA:-}"
	validate_sha256 "$input_sha" "bundle input fingerprint"
	validate_generation "$generation"
	validate_latest_reference toolbox "$toolbox_latest"
	validate_latest_reference kernel-build "$build_latest"
	validate_latest_reference apt-client "$client_latest"
	verify_image toolbox "$toolbox" "$base_image" "$llvm_major" "$input_sha" "$generation" >/dev/null ||
		dkc::die "toolbox image is not the requested local bundle"
	verify_image kernel-build "$build" "$base_image" "$llvm_major" "$input_sha" "$generation" >/dev/null ||
		dkc::die "kernel-build image is not the requested local bundle"
	verify_image apt-client "$client" "$base_image" "$llvm_major" "$input_sha" "$generation" >/dev/null ||
		dkc::die "APT-client image is not the requested local bundle"
	for image in "$toolbox" "$build" "$client"; do
		[ "$(inspect_label "$image" org.opencontainers.image.revision)" = "$GITHUB_SHA" ] ||
			dkc::die "container image source revision differs from the workflow commit"
	done
	dkc::info "all local images passed; publishing the three latest tags"
	podman push "$toolbox" "docker://${toolbox_latest}"
	podman push "$build" "docker://${build_latest}"
	podman push "$client" "docker://${client_latest}"
	dkc::ok "published the complete container image bundle"
}

resolved_digest_reference() {
	local role="$1" latest="$2" digest
	validate_role "$role"
	digest="$(inspect_format "$latest" '{{.Digest}}')" || return 1
	[[ "$digest" =~ ^sha256:[0-9a-f]{64}$ ]] || return 1
	printf '%s@%s\n' "${latest%:latest}" "$digest"
}

resolve_once() {
	local expected_generation="$1" toolbox_latest="$2" build_latest="$3" client_latest="$4"
	local toolbox_generation build_generation client_generation
	local toolbox_input build_input client_input
	validate_latest_reference toolbox "$toolbox_latest"
	validate_latest_reference kernel-build "$build_latest"
	validate_latest_reference apt-client "$client_latest"
	podman pull "$toolbox_latest" || return 1
	podman pull "$build_latest" || return 1
	podman pull "$client_latest" || return 1
	toolbox_generation="$(verify_published_image toolbox "$toolbox_latest")" || return 1
	build_generation="$(verify_published_image kernel-build "$build_latest")" || return 1
	client_generation="$(verify_published_image apt-client "$client_latest")" || return 1
	toolbox_input="$(inspect_label "$toolbox_latest" "$input_label")" || return 1
	build_input="$(inspect_label "$build_latest" "$input_label")" || return 1
	client_input="$(inspect_label "$client_latest" "$input_label")" || return 1
	if [ "$toolbox_generation" != "$build_generation" ] || [ "$toolbox_generation" != "$client_generation" ]; then
		dkc::warn "latest tags belong to different publication generations"
		return 1
	fi
	if [ "$toolbox_input" != "$build_input" ] || [ "$toolbox_input" != "$client_input" ]; then
		dkc::warn "latest tags do not describe one image input bundle"
		return 1
	fi
	if [ -n "$expected_generation" ] && [ "$toolbox_generation" != "$expected_generation" ]; then
		dkc::warn "latest tags do not yet expose the expected publication generation"
		return 1
	fi
	RESOLVED_GENERATION="$toolbox_generation"
	RESOLVED_INPUT="$toolbox_input"
	RESOLVED_TOOLBOX="$(resolved_digest_reference toolbox "$toolbox_latest")" || return 1
	RESOLVED_BUILD="$(resolved_digest_reference kernel-build "$build_latest")" || return 1
	RESOLVED_CLIENT="$(resolved_digest_reference apt-client "$client_latest")" || return 1
	export RESOLVED_GENERATION RESOLVED_INPUT RESOLVED_TOOLBOX RESOLVED_BUILD RESOLVED_CLIENT
}

resolve_bundle() {
	local expected_generation="$1" timeout_seconds="$2" interval_seconds="$3" output="$4"
	local toolbox_latest="$5" build_latest="$6" client_latest="$7"
	local started now elapsed
	[ -z "$expected_generation" ] || validate_generation "$expected_generation"
	[[ "$timeout_seconds" =~ ^[0-9]+$ ]] || dkc::die "resolve timeout must be an integer"
	[[ "$interval_seconds" =~ ^[0-9]+$ ]] || dkc::die "resolve interval must be an integer"
	[ "$timeout_seconds" -gt 0 ] || dkc::die "resolve timeout must be positive"
	[ "$interval_seconds" -gt 0 ] || dkc::die "resolve interval must be positive"
	[ -n "$output" ] || dkc::die "resolve output path is empty"
	started="$(date +%s)"
	while ! resolve_once "$expected_generation" "$toolbox_latest" "$build_latest" "$client_latest"; do
		now="$(date +%s)"
		elapsed=$((now - started))
		if [ "$elapsed" -ge "$timeout_seconds" ]; then
			dkc::die "no coherent public latest image bundle became available within ${timeout_seconds}s; verify all three GHCR packages are public"
		fi
		dkc::warn "container image bundle is not coherent yet; retrying in ${interval_seconds}s"
		sleep "$interval_seconds"
	done
	{
		printf 'bundle_input_sha256=%s\n' "$RESOLVED_INPUT"
		printf 'bundle_generation=%s\n' "$RESOLVED_GENERATION"
		printf 'toolbox_image=%s\n' "$RESOLVED_TOOLBOX"
		printf 'build_image=%s\n' "$RESOLVED_BUILD"
		printf 'apt_client_image=%s\n' "$RESOLVED_CLIENT"
	} >>"$output"
	dkc::ok "resolved coherent container image generation ${RESOLVED_GENERATION}"
}

[ "$#" -ge 1 ] || usage
command="$1"
shift

case "$command" in
fingerprint)
	[ "$#" -eq 2 ] || usage
	fingerprint "$@"
	;;
paths)
	[ "$#" -eq 1 ] || usage
	list_paths "$@"
	;;
build)
	[ "$#" -eq 7 ] || usage
	dkc::refuse_root
	dkc::require_cmd podman stat sha256sum
	build_image "$@"
	;;
ensure)
	[ "$#" -eq 2 ] || usage
	dkc::refuse_root
	dkc::require_cmd podman
	ensure_image "$@"
	;;
ensure-base)
	[ "$#" -eq 1 ] || usage
	dkc::refuse_root
	dkc::require_cmd podman
	ensure_base "$@"
	;;
push-bundle)
	[ "$#" -eq 10 ] || usage
	dkc::refuse_root
	dkc::require_cmd podman
	push_bundle "$@"
	;;
resolve)
	[ "$#" -eq 7 ] || usage
	dkc::refuse_root
	dkc::require_cmd podman sleep
	resolve_bundle "$@"
	;;
*) usage ;;
esac

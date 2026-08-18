#!/usr/bin/env bash
# Fetch and verify the immutable Debian cloud image used by QEMU tests.

# shellcheck source=scripts/lib/common.sh
source "$(dirname "${BASH_SOURCE[0]}")/lib/common.sh"

dkc::refuse_root
dkc::require_cmd qemu-img jq realpath sha512sum
dkc::install_cleanup_trap

[ "$#" -eq 1 ] || dkc::die "usage: fetch-qemu-image.sh <image-config>"
config="$(realpath "$1")"
case "$config" in
"${DKC_ROOT}/config/"*) ;;
*) dkc::die "image configuration must be inside config/" ;;
esac

# shellcheck disable=SC1090
source "$config"
: "${DKC_QEMU_IMAGE_URL:?missing image URL}"
: "${DKC_QEMU_IMAGE_FILENAME:?missing image filename}"
: "${DKC_QEMU_IMAGE_SHA512:?missing image SHA-512}"
: "${DKC_QEMU_IMAGE_FORMAT:?missing image format}"

[[ "$DKC_QEMU_IMAGE_URL" =~ ^https://cloud\.debian\.org/images/cloud/trixie/[0-9]{8}-[0-9]+/[A-Za-z0-9._-]+\.qcow2$ ]] ||
	dkc::die "QEMU image URL is not a pinned official Debian Trixie qcow2"
[[ "$DKC_QEMU_IMAGE_FILENAME" =~ ^[A-Za-z0-9._-]+\.qcow2$ ]] ||
	dkc::die "unsafe QEMU image filename"
[[ "$DKC_QEMU_IMAGE_SHA512" =~ ^[0-9a-f]{128}$ ]] || dkc::die "invalid QEMU image SHA-512"
[ "$DKC_QEMU_IMAGE_FORMAT" = qcow2 ] || dkc::die "only a qcow2 base image is supported"
[ "${DKC_QEMU_IMAGE_URL##*/}" = "$DKC_QEMU_IMAGE_FILENAME" ] ||
	dkc::die "QEMU image URL and filename disagree"

cache_dir="${DKC_CACHE_DIR}/qemu"
image="${cache_dir}/${DKC_QEMU_IMAGE_FILENAME}"
mkdir -p "$cache_dir"

if [ -f "$image" ]; then
	got="$(sha512sum "$image" | awk '{print $1}')"
	if [ "$got" = "$DKC_QEMU_IMAGE_SHA512" ]; then
		dkc::log "cached QEMU image verified sha512=${got}"
	else
		quarantine="${DKC_RUN_DIR}/invalid-${DKC_QEMU_IMAGE_FILENAME}"
		mv "$image" "$quarantine"
		dkc::register_resource path "$quarantine"
		dkc::warn "quarantined a cached image with the wrong checksum"
	fi
fi

if [ ! -f "$image" ]; then
	dkc::fetch_digest "$DKC_QEMU_IMAGE_URL" "$image" sha512 "$DKC_QEMU_IMAGE_SHA512"
fi

# The image is a cacheable input, never a writable guest disk. Per-run qcow2
# overlays provide all guest persistence and are discarded after validation.
chmod 0444 "$image"

info="$(qemu-img info --output=json "$image")"
[ "$(jq -r '.format' <<<"$info")" = "$DKC_QEMU_IMAGE_FORMAT" ] ||
	dkc::die "downloaded image format differs from the lock"
virtual_size="$(jq -r '.["virtual-size"]' <<<"$info")"
[[ "$virtual_size" =~ ^[0-9]+$ ]] || dkc::die "QEMU image has no valid virtual size"
[ "$virtual_size" -ge 2147483648 ] || dkc::die "QEMU image is unexpectedly small"

dkc::ok "immutable QEMU base image ready: ${image}"
printf '%s\n' "$image"

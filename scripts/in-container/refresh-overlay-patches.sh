#!/usr/bin/env bash
# Regenerate the packaging overlay against the current Debian kernel source.
#
# Runs inside the build container. Emits a tar of one source-profile patch
# directory on stdout so the caller can unpack it over the repository, because
# the container has no write access to it.
#
# Every edit is anchored on exact text from the Debian source. When Debian
# changes one of those lines the generator fails and names the anchor, which is
# the point: an overlay that applies with fuzz into a subtly different tree is
# worse than one that refuses to regenerate.

set -Eeuo pipefail

ORIG_TAR_URL="${1:?orig tar url required}"
ORIG_TAR_SHA256="${2:?orig tar sha256 required}"
DEBIAN_TAR_URL="${3:?debian tar url required}"
DEBIAN_TAR_SHA256="${4:?debian tar sha256 required}"

# /work, not /tmp: the work area must allow execution and hold an unpacked tree.
work="$(mktemp -d /work/regen-XXXXXX)"
trap 'rm -rf -- "$work"' EXIT

PYTHONPATH=/work/src python3 -m dkc.cli.download \
	--user-agent dkc-source-audit/1 "$ORIG_TAR_URL" "$work/orig.tar.xz"
PYTHONPATH=/work/src python3 -m dkc.cli.download \
	--user-agent dkc-source-audit/1 "$DEBIAN_TAR_URL" "$work/debian.tar.xz"
echo "${ORIG_TAR_SHA256}  ${work}/orig.tar.xz" | sha256sum -c - >&2
echo "${DEBIAN_TAR_SHA256}  ${work}/debian.tar.xz" | sha256sum -c - >&2
tar -C "$work" -xf "${work}/orig.tar.xz"
mapfile -d '' -t source_roots < <(
	find "$work" -mindepth 1 -maxdepth 1 -type d -name 'linux-*' -print0
)
[ "${#source_roots[@]}" -eq 1 ] || {
	echo "orig source archive did not contain one Linux source root" >&2
	exit 1
}
source_root="${source_roots[0]}"
test -f "${source_root}/arch/x86/Makefile"
tar -C "$source_root" -xf "${work}/debian.tar.xz"

# The patches belong to the source profile that covers this exact version, so
# regeneration can never silently overwrite another kernel series' overlay.
source_version="$(dpkg-parsechangelog -l"${source_root}/debian/changelog" -SVersion)"
profile_id="$(PYTHONPATH=/work/src python3 -m dkc.sourceprofile \
	/work/src "$source_version" id)"
out="${work}/patches/${profile_id}"
mkdir -p "$out"
PYTHONPATH=/work/src python3 -m dkc.cli.generate_overlay_patches \
	"$source_root" "$out"

tar --create --file=- --directory="${work}/patches" "$profile_id"

#!/usr/bin/env bash
# Regenerate the packaging overlay against the current Debian kernel source.
#
# Runs inside the build container. Emits a tar of the patch directory on stdout
# so the caller can unpack it over the repository, because the container has no
# write access to it.
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
LLVM_MAJOR="${5:?llvm major required}"

# /work, not /tmp: the work area must allow execution and hold an unpacked tree.
work="$(mktemp -d /work/regen-XXXXXX)"
trap 'rm -rf -- "$work"' EXIT

python3 - "$ORIG_TAR_URL" "$DEBIAN_TAR_URL" "$work" <<'PY'
import pathlib
import sys
import time
import urllib.request


def fetch(url: str, output: pathlib.Path) -> None:
    partial = output.with_suffix(output.suffix + ".partial")
    for attempt in range(1, 5):
        try:
            request = urllib.request.Request(
                url, headers={"User-Agent": "dkc-source-audit/1"}
            )
            with urllib.request.urlopen(request, timeout=180) as response, partial.open(
                "wb"
            ) as stream:
                while chunk := response.read(1024 * 1024):
                    stream.write(chunk)
            partial.replace(output)
            return
        except Exception as error:
            partial.unlink(missing_ok=True)
            if attempt == 4:
                raise
            print(
                f"download attempt {attempt}/4 failed for {url}: {error}; retrying",
                file=sys.stderr,
                flush=True,
            )
            time.sleep(attempt)


root = pathlib.Path(sys.argv[3])
for url, name in ((sys.argv[1], "orig.tar.xz"), (sys.argv[2], "debian.tar.xz")):
    fetch(url, root / name)
PY
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

out="${work}/patches"
mkdir -p "$out"
for name in \
	0001-select-llvm-toolchain.patch \
	0002-drive-kbuild-with-llvm.patch \
	0003-disable-random-module-signing.patch \
	0004-x86-64-flavours.patch \
	0005-dkc-package-namespace.patch; do
	python3 /work/src/scripts/in-container/generate-overlay-patches.py \
		"$source_root" "$LLVM_MAJOR" "$name" >"${out}/${name}"
	printf 'generated %s (%s lines)\n' "$name" "$(wc -l <"${out}/${name}")" >&2
done

tar --create --file=- --directory="$out" .

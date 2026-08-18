#!/usr/bin/env bash
# Fetch and verify the complete source inventory before the build loses network.

set -Eeuo pipefail

[ "$#" -eq 14 ] || {
	echo "usage: stage-one-build.sh dsc-url dsc-name dsc-sha dsc-size orig-url orig-name orig-sha orig-size debian-url debian-name debian-sha debian-size source-version llvm-major" >&2
	exit 2
}

dsc_url="$1" dsc_name="$2" dsc_sha="$3" dsc_size="$4"
orig_url="$5" orig_name="$6" orig_sha="$7" orig_size="$8"
debian_url="$9" debian_name="${10}" debian_sha="${11}" debian_size="${12}"
source_version="${13}" llvm_major="${14}"

case "$source_version" in
*[!0-9A-Za-z.+:~_-]* | "")
	echo "unsafe source version: $source_version" >&2
	exit 2
	;;
esac
[[ "$llvm_major" =~ ^[0-9]+$ ]] || {
	echo "invalid LLVM major" >&2
	exit 2
}

validate_member() {
	local url="$1" name="$2" suffix="$3"
	[[ "$name" =~ ^[A-Za-z0-9][A-Za-z0-9._+~-]*$ ]] || {
		echo "unsafe source member name" >&2
		exit 2
	}
	[[ "$name" == *"$suffix" ]] || {
		echo "source member has the wrong type" >&2
		exit 2
	}
	[ "${url##*/}" = "$name" ] || {
		echo "source member name differs from its URL" >&2
		exit 2
	}
}
validate_member "$dsc_url" "$dsc_name" .dsc
validate_member "$orig_url" "$orig_name" .orig.tar.xz
validate_member "$debian_url" "$debian_name" .debian.tar.xz

inputs=/work/inputs
mkdir -p "$inputs"

# The Debian source lock is not enough to identify the build: the streamed
# repository supplies the overlay, profiles, and verifier. Preserve a compact
# deterministic hash inventory before any of those files execute in the
# offline phase. The host supplies only current project inputs.
(
	cd /work/repo
	find . -type f -print0 | sort -z | xargs -0 sha256sum
) >"$inputs/repository-inputs.sha256"

fetch() {
	local url="$1" output="$2" expected_sha="$3" expected_size="$4"
	python3 - "$url" "$output" <<'PY'
import pathlib
import sys
import time
import urllib.request

url, output = sys.argv[1:]
target = pathlib.Path(output)
partial = target.with_suffix(target.suffix + ".partial")
for attempt in range(1, 5):
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "dkc-source-stage/1"})
        with urllib.request.urlopen(request, timeout=180) as response, partial.open("wb") as stream:
            while chunk := response.read(1024 * 1024):
                stream.write(chunk)
        partial.replace(target)
        break
    except Exception:
        partial.unlink(missing_ok=True)
        if attempt == 4:
            raise
        time.sleep(attempt)
PY
	printf '%s  %s\n' "$expected_sha" "$output" | sha256sum -c -
	actual_size="$(stat -c %s "$output")"
	[ "$actual_size" = "$expected_size" ] || {
		echo "size mismatch for ${output}: ${actual_size} != ${expected_size}" >&2
		exit 1
	}
}

fetch "$dsc_url" "$inputs/$dsc_name" "$dsc_sha" "$dsc_size"
fetch "$orig_url" "$inputs/$orig_name" "$orig_sha" "$orig_size"
fetch "$debian_url" "$inputs/$debian_name" "$debian_sha" "$debian_size"

# dpkg-source validates the member hashes declared by the authenticated .dsc.
# The archive Sources signature and the exact .dsc hash were established by
# discovery; a maintainer's optional OpenPGP signature is not the archive trust
# root and is deliberately not substituted for that proof.
validate=/work/source-validation
mkdir -p "$validate"
validated_source="$validate/source"
dpkg-source -x "$inputs/$dsc_name" "$validated_source" >/dev/null
[ "$(dpkg-parsechangelog -l"$validated_source/debian/changelog" -SVersion)" = \
	"$source_version" ] || {
	echo "extracted Debian source version differs from discovery" >&2
	exit 1
}
for patch in /work/repo/debian-overlay/patches/*.patch; do
	patch -d "$validated_source" -p1 --batch --forward --silent <"$patch"
done
# shellcheck disable=SC1091,SC2153  # repository file streamed into the container
. /work/repo/config/build-profiles
# shellcheck disable=SC2153  # assigned by the sourced build-profiles file
export DEB_BUILD_PROFILES="$DKC_BUILD_PROFILES"
(
	cd "$validated_source"
	python3 debian/bin/gencontrol.py >/dev/null
	dpkg-checkbuilddeps debian/control
	dpkg-parsechangelog -STimestamp >"$inputs/source-date-epoch"
)
rm -rf -- "$validate"

cp /usr/share/dkc/packages.tsv "$inputs/build-image-packages.tsv"
cp /usr/share/dkc/toolchain.env "$inputs/toolchain.env"
cp /usr/share/dkc/apt-indexes.sha256 "$inputs/apt-indexes.sha256"
grep -qx 'rust_source=debian' "$inputs/toolchain.env" || {
	echo "the reproducible build requires the Debian-packaged Rust toolchain captured by the .deb lock" >&2
	exit 1
}

cp /usr/share/dkc/apt-indexes.sha256 "$inputs/staging-apt-indexes.sha256"

# The immutable image is one independently versioned input. Do not try to
# recreate it later from Debian's moving mirrors: package versions can leave a
# suite between image publication and a kernel build. Debian's container APT
# cleanup hook is disabled when the image is built, so every package downloaded
# on top of the digest-pinned base remains available here. Hash those retained
# archives; packages inherited unchanged from the base are already covered by
# the base digest in the publication identity.
python3 /work/repo/scripts/in-container/lock-build-environment.py \
	--packages "$inputs/build-image-packages.tsv" \
	--archives /var/cache/apt/archives \
	--output "$inputs/build-image-debs.tsv" >&2

python3 - "$inputs" "$source_version" "$llvm_major" \
	"$dsc_url" "$dsc_name" "$dsc_sha" "$dsc_size" \
	"$orig_url" "$orig_name" "$orig_sha" "$orig_size" \
	"$debian_url" "$debian_name" "$debian_sha" "$debian_size" <<'PY'
import json
import pathlib
import sys

(root, version, llvm_major, dsc_url, dsc_name, dsc_sha, dsc_size, orig_url,
 orig_name, orig_sha, orig_size, debian_url, debian_name, debian_sha,
 debian_size) = sys.argv[1:]
epoch = int((pathlib.Path(root) / "source-date-epoch").read_text().strip())
record = {
    "schema_version": 2,
    "source": "linux",
    "version": version,
    "source_date_epoch": epoch,
    "llvm_major": int(llvm_major),
    "files": [
        {"name": dsc_name, "url": dsc_url, "sha256": dsc_sha, "size": int(dsc_size)},
        {"name": orig_name, "url": orig_url, "sha256": orig_sha, "size": int(orig_size)},
        {"name": debian_name, "url": debian_url, "sha256": debian_sha, "size": int(debian_size)},
    ],
}
(pathlib.Path(root) / "source-inventory.json").write_text(
    json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8"
)
PY

echo "source staging PASS: exact inventory verified; SOURCE_DATE_EPOCH=$(cat "$inputs/source-date-epoch")" >&2

#!/usr/bin/env bash
# Build the archive key bundle as both a binary and a reconstructible source package.

set -Eeuo pipefail

if [ "$#" -ne 7 ]; then
	printf 'usage: build-archive-keyring.sh <public-keyring> <primary-fingerprint> <signing-subkeys> <version> <key-epoch> <work> <output>\n' >&2
	exit 2
fi
keyring="$(realpath "$1")"
primary_fingerprint="$(realpath "$2")"
signing_subkeys="$(realpath "$3")"
version="$4"
key_epoch="$5"
work="$6"
output="$7"
[[ "$version" =~ ^[0-9][0-9A-Za-z.+~_-]*$ ]]
[[ "$key_epoch" =~ ^[1-9][0-9]*$ ]]
test -s "$keyring" -a -s "$primary_fingerprint" -a -s "$signing_subkeys"
test ! -e "$work" -a ! -e "$output"
mkdir -p "$work/dkc-archive-keyring-${version}/debian/source" "$output"
source_root="$work/dkc-archive-keyring-${version}"

install -D -m 0644 "$keyring" \
	"$source_root/keys/dkc-archive-keyring.gpg"
install -D -m 0644 "$primary_fingerprint" \
	"$source_root/keys/archive-primary.fingerprint"
install -D -m 0644 "$signing_subkeys" \
	"$source_root/keys/archive-signing-subkeys.fingerprints"
cat >"$source_root/debian/source/format" <<'EOF'
3.0 (native)
EOF
cat >"$source_root/debian/control" <<'EOF'
Source: dkc-archive-keyring
Section: misc
Priority: optional
Maintainer: DKC Kernel Maintainers <build@dkc.invalid>
Build-Depends: debhelper-compat (= 13)
Rules-Requires-Root: no
Standards-Version: 4.7.2
Homepage: https://github.com/kogeler/dkc-linux
Vcs-Git: https://github.com/kogeler/dkc-linux.git
Vcs-Browser: https://github.com/kogeler/dkc-linux

Package: dkc-archive-keyring
Architecture: all
Multi-Arch: foreign
Depends: ${misc:Depends}
Description: OpenPGP keys for the DKC package archive
 This package installs the public keys used by apt-secure to authenticate
 metadata from the DKC package archive. It grants no kernel, module, or UEFI
 Secure Boot trust.
EOF
cat >"$source_root/debian/rules" <<'EOF'
#!/usr/bin/make -f
%:
	dh $@
EOF
chmod 0755 "$source_root/debian/rules"
cat >"$source_root/debian/install" <<'EOF'
keys/dkc-archive-keyring.gpg usr/share/keyrings
keys/archive-primary.fingerprint usr/share/dkc-archive-keyring
keys/archive-signing-subkeys.fingerprints usr/share/dkc-archive-keyring
EOF
cat >"$source_root/debian/copyright" <<'EOF'
Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/
Upstream-Name: dkc-archive-keyring
Source: https://github.com/kogeler/dkc-linux

Files: keys/*
Copyright: none
License: public-key-material
 Public keys and their fingerprints are factual cryptographic material and
 are not granted authority beyond verification of the DKC package archive.

Files: *
Copyright: 2026 kogeler
License: MIT
 Permission is hereby granted, free of charge, to any person obtaining a copy
 of this software and associated documentation files (the "Software"), to deal
 in the Software without restriction, including without limitation the rights
 to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
 copies of the Software, and to permit persons to whom the Software is
 furnished to do so, subject to the following conditions:
 .
 The above copyright notice and this permission notice shall be included in all
 copies or substantial portions of the Software.
 .
 THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
 IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
 FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
 AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
 LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
 OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
 SOFTWARE.
EOF
changelog_date="$(date --utc --date="@${key_epoch}" --rfc-email)"
cat >"$source_root/debian/changelog" <<EOF
dkc-archive-keyring (${version}) trixie; urgency=medium

  * Publish the reviewed archive verification keys.

 -- DKC Kernel Maintainers <build@dkc.invalid>  ${changelog_date}
EOF
find "$source_root" -exec touch --date="@${key_epoch}" {} +

export SOURCE_DATE_EPOCH="$key_epoch"
export DEB_BUILD_OPTIONS=noautodbgsym
(
	cd "$source_root"
	dpkg-buildpackage --build=source --no-sign
)
version_filename="${version#*:}"
dsc="$work/dkc-archive-keyring_${version_filename}.dsc"
test -f "$dsc"
dpkg-source -x "$dsc" "$work/reconstructed" >/dev/null
(
	cd "$work/reconstructed"
	dpkg-buildpackage --build=binary --no-sign
)

expected=(
	"dkc-archive-keyring_${version_filename}.dsc"
	"dkc-archive-keyring_${version_filename}.tar.xz"
	"dkc-archive-keyring_${version_filename}_source.changes"
	"dkc-archive-keyring_${version_filename}_source.buildinfo"
	"dkc-archive-keyring_${version_filename}_all.deb"
)
for name in "${expected[@]}"; do
	test -f "$work/$name"
	cp --reflink=auto --preserve=mode,timestamps "$work/$name" "$output/$name"
done
cmp "$keyring" \
	<(
		dpkg-deb --fsys-tarfile "$output/dkc-archive-keyring_${version_filename}_all.deb" |
			tar -xOf - ./usr/share/keyrings/dkc-archive-keyring.gpg
	)
(
	cd "$output"
	sha256sum "${expected[@]}" >bundle.sha256
)

#!/usr/bin/env bash
# Prove apt-secure, by-hash, deb-src, rotation, and negative signature handling.

set -Eeuo pipefail

if [ "$#" -ne 2 ]; then
	printf 'usage: test-signed-repository.sh <repository> <evidence>\n' >&2
	exit 2
fi
repository="$(realpath "$1")"
evidence="$2"
repo_root="$(realpath "$(dirname "$0")/../..")"
mkdir -p "$evidence"
keyring="$repository/keys/dkc-archive-keyring.gpg"
test -f "$keyring"
command -v apt-get >/dev/null
command -v dpkg-source >/dev/null
command -v gpgv >/dev/null

export DEBIAN_FRONTEND=noninteractive
export SYSTEMD_OFFLINE=1
printf '#!/bin/sh\nexit 101\n' >/usr/sbin/policy-rc.d
chmod 0755 /usr/sbin/policy-rc.d

work_base="${DKC_CLIENT_WORK:-/tmp}"
test -d "$work_base" -a -w "$work_base"
work="$(mktemp -d "$work_base/signed-client-XXXXXX")"
negative_root=""
cleanup() {
	[ -z "$negative_root" ] || rm -rf -- "$negative_root"
	rm -rf -- "$work"
}
trap cleanup EXIT

gpgv --keyring "$keyring" "$repository/dists/trixie/InRelease" \
	>"$evidence/inrelease-gpgv.log" 2>&1
gpgv --keyring "$keyring" "$repository/dists/trixie/Release.gpg" \
	"$repository/dists/trixie/Release" >"$evidence/release-gpgv.log" 2>&1
gpgv --keyring "$keyring" "$repository/manifest.json.asc" \
	"$repository/manifest.json" >"$evidence/manifest-gpgv.log" 2>&1
gpgv --output "$evidence/state-current.json" --keyring "$keyring" \
	"$repository/state/current.asc" \
	>"$evidence/state-gpgv.log" 2>&1
gpgv --keyring "$keyring" "$repository/SHA256SUMS.asc" \
	"$repository/SHA256SUMS" >"$evidence/checksums-gpgv.log" 2>&1
(
	cd "$repository"
	sha256sum --check SHA256SUMS
) >"$evidence/root-checksums.log"

mapfile -t immutable_manifests < <(
	find "$repository/state/publications" -type f -name manifest.json -print
)
mapfile -t immutable_transactions < <(
	find "$repository/state/transactions" -type f -name record.json -print
)
[ "${#immutable_manifests[@]}" -eq 1 ]
[ "${#immutable_transactions[@]}" -eq 1 ]
gpgv --keyring "$keyring" "${immutable_manifests[0]}.asc" \
	"${immutable_manifests[0]}" >"$evidence/immutable-manifest-gpgv.log" 2>&1
gpgv --keyring "$keyring" "${immutable_transactions[0]}.asc" \
	"${immutable_transactions[0]}" >"$evidence/transaction-gpgv.log" 2>&1

PYTHONPATH="$repo_root" python3 - "$repository" "$evidence/state-current.json" <<'PY'
import hashlib
import json
import pathlib
import sys

from dkc.schema import validate


def sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


root = pathlib.Path(sys.argv[1]).resolve()
pointer = json.loads(pathlib.Path(sys.argv[2]).read_text(encoding="utf-8"))
validate("state-pointer", pointer)
manifest_path = root / pointer["manifest_key"]
if not manifest_path.is_file() or sha256(manifest_path) != pointer["manifest_sha256"]:
    raise SystemExit("state pointer does not identify the immutable manifest bytes")
manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
validate("publication-manifest", manifest)
if (
    manifest["publication_id"] != pointer["publication_id"]
    or manifest["generation"] != pointer["generation"]
    or (root / "manifest.json").read_bytes() != manifest_path.read_bytes()
):
    raise SystemExit("state pointer, immutable manifest, and root manifest disagree")

inrelease = root / "dists/trixie/InRelease"
if manifest["apt_metadata"]["inrelease_sha256"] != sha256(inrelease):
    raise SystemExit("manifest does not bind the committed InRelease")
for relative, expected in manifest["apt_metadata"]["index_hashes"].items():
    if sha256(root / relative) != expected:
        raise SystemExit(f"manifest index hash differs: {relative}")

artifact_keys = {item["key"] for item in manifest["artifacts"]}
if artifact_keys != set(manifest["live_objects"]):
    raise SystemExit("local current publication does not mark every artifact live")
expected_artifacts = {
    path.relative_to(root).as_posix()
    for prefix in ("pool", "dists", "keys")
    for path in (root / prefix).rglob("*")
    if path.is_file()
}
if artifact_keys != expected_artifacts:
    raise SystemExit("manifest artifact graph differs from the repository graph")
for item in manifest["artifacts"]:
    path = root / item["key"]
    if not path.is_file() or path.stat().st_size != item["size"] or sha256(path) != item["sha256"]:
        raise SystemExit(f"manifest artifact differs: {item['key']}")

transaction_path = root / (
    f"state/transactions/{manifest['transaction_id']}/record.json"
)
transaction = json.loads(transaction_path.read_text(encoding="utf-8"))
validate("transaction", transaction)
if (
    transaction["publication_id"] != manifest["publication_id"]
    or transaction["expected_generation"] != manifest["generation"]
    or transaction["intended_inrelease_sha256"] != sha256(inrelease)
):
    raise SystemExit("transaction, manifest, and InRelease disagree")
PY

mapfile -t primary_fingerprints <"$repository/keys/archive-primary.fingerprint"
mapfile -t signing_subkeys <"$repository/keys/archive-signing-subkeys.fingerprints"
[ "${#primary_fingerprints[@]}" -eq 1 ]
[ "${#signing_subkeys[@]}" -ge 1 ]

cat >/etc/apt/sources.list.d/dkc.sources <<EOF
Types: deb deb-src
URIs: file:${repository}
Suites: trixie
Components: main
Signed-By: ${keyring}
EOF
rm -f /etc/apt/sources.list /etc/apt/sources.list.d/debian.sources
if ! apt-get update -o Debug::pkgAcquire::Worker=true \
	>"$evidence/apt-update.log" 2>&1; then
	printf 'apt could not read the signed repository; apt-update.log is retained\n' >&2
	exit 1
fi
if ! grep -Eq 'by-hash(%2f|/)SHA256' "$evidence/apt-update.log"; then
	printf 'apt did not request the advertised by-hash indexes; apt-update.log is retained\n' >&2
	exit 1
fi
release_meta_packages=(
	dkc-linux-base-v2-amd64
	dkc-linux-base-v3-amd64
	dkc-linux-headers-v2-amd64
	dkc-linux-headers-v3-amd64
	dkc-linux-image-v2-amd64
	dkc-linux-image-v3-amd64
)
release_image_metas=(
	dkc-linux-image-v2-amd64
	dkc-linux-image-v3-amd64
)
current_dkc_version="$(
	python3 - "$repository/manifest.json" <<'PY'
import json
import pathlib
import sys

manifest = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
versions = set(manifest["meta_packages"].values())
if len(versions) != 1:
    raise SystemExit("signed manifest does not identify one current DKC version")
print(versions.pop())
PY
)"
test -n "$current_dkc_version"
apt-cache policy dkc-archive-keyring "${release_meta_packages[@]}" \
	>"$evidence/apt-policy.txt"
keyring_candidate="$(
	apt-cache policy dkc-archive-keyring |
		sed -n 's/^[[:space:]]*Candidate: //p'
)"
if [ -z "$keyring_candidate" ] || [ "$keyring_candidate" = '(none)' ]; then
	printf 'signed archive does not expose the archive keyring\n' >&2
	exit 1
fi
for package in "${release_meta_packages[@]}"; do
	candidate="$(
		apt-cache policy "$package" |
			sed -n 's/^[[:space:]]*Candidate: //p'
	)"
	if [ "$candidate" != "$current_dkc_version" ]; then
		printf '%s candidate %s differs from signed current version %s\n' \
			"$package" "$candidate" "$current_dkc_version" >&2
		exit 1
	fi
done

# The earlier package-matrix clients exercise the complete package lifecycle
# against an unsigned local fixture. This independent client proves that APT
# can also resolve and install both release kernels through the final signed
# indexes, with Debian network sources removed and networking disabled.
if ! apt-get install -y --no-install-recommends "${release_image_metas[@]}" \
	>"$evidence/apt-install-release-kernels.log" 2>&1; then
	printf 'release kernels did not install; apt-install-release-kernels.log is retained\n' >&2
	exit 1
fi
dpkg-query -W -f='${binary:Package}\t${db:Status-Status}\n' |
	awk -F '\t' '$2 == "installed" { print $1 }' |
	sort >"$evidence/installed-packages.txt"
for flavor in v2 v3; do
	meta="dkc-linux-image-${flavor}-amd64"
	if [ "$(dpkg-query -W -f='${Version}' "$meta")" != "$current_dkc_version" ]; then
		printf '%s did not install the signed current version\n' "$meta" >&2
		exit 1
	fi
	# Read the installed current metapackage relationship, not every retained
	# ABI name in Packages or every old kernel that may coexist on the client.
	mapfile -t installed_krels < <(
		dpkg-query -W -f='${Depends}\n' "$meta" |
			tr ',' '\n' |
			sed -n "s/^[[:space:]]*dkc-linux-image-\\([^[:space:](]*-${flavor}-amd64\\)[[:space:]]*(=.*/\\1/p"
	)
	[ "${#installed_krels[@]}" -eq 1 ]
	krel="${installed_krels[0]}"
	[[ "$krel" =~ ^[A-Za-z0-9][A-Za-z0-9.+~-]*-${flavor}-amd64$ ]]
	for package in \
		"dkc-linux-base-${flavor}-amd64" \
		"dkc-linux-image-${flavor}-amd64" \
		"dkc-linux-base-${krel}" \
		"dkc-linux-binary-${krel}" \
		"dkc-linux-modules-${krel}" \
		"dkc-linux-image-${krel}"; do
		grep -qx "$package" "$evidence/installed-packages.txt"
	done
	test -s "/boot/vmlinuz-${krel}"
	test -s "/boot/config-${krel}"
	test -s "/boot/System.map-${krel}"
	test -s "/boot/initrd.img-${krel}"
	test -d "/lib/modules/${krel}/kernel"
done

sources="$work/sources"
mkdir "$sources"
chmod 0777 "$sources"
(
	cd "$sources"
	apt-get source dkc-linux >"$evidence/apt-source-linux.log" 2>&1
	apt-get source dkc-archive-keyring >"$evidence/apt-source-keyring.log" 2>&1
)
find "$sources" -mindepth 1 -maxdepth 1 -printf '%y\t%f\t%l\n' |
	sort >"$evidence/source-download-inventory.tsv"
mapfile -t linux_trees < <(
	find "$sources" -mindepth 1 -maxdepth 1 -type d -name 'dkc-linux-*' -print
)
[ "${#linux_trees[@]}" -eq 1 ]
linux_source_version="$(
	dpkg-parsechangelog -l"${linux_trees[0]}/debian/changelog" -SVersion
)"
if [ "$linux_source_version" != "$current_dkc_version" ]; then
	printf 'APT selected source version %s instead of signed current version %s\n' \
		"$linux_source_version" "$current_dkc_version" >&2
	exit 1
fi
printf '%s\n' "$linux_source_version" >"$evidence/source-version.txt"
mapfile -t keyring_trees < <(
	find "$sources" -mindepth 1 -maxdepth 1 -type d -name 'dkc-archive-keyring-*' -print
)
[ "${#keyring_trees[@]}" -eq 1 ]
# The file method normally links source members back into the signed pool.
# Accept only paths with regular referents, then constrain the resolved target.
mapfile -t original_tarballs < <(
	find "$sources" -maxdepth 1 -xtype f \
		-name 'dkc-linux_*.orig.tar.xz' -print
)
[ "${#original_tarballs[@]}" -eq 1 ]
original_tarball="$(realpath "${original_tarballs[0]}")"
case "$original_tarball" in
"$sources"/* | "$repository"/pool/main/d/dkc-linux/*) ;;
*)
	printf 'APT source tarball resolved outside the verified source boundaries\n' >&2
	exit 1
	;;
esac

rebuild="$work/rebuild"
mkdir "$rebuild"
cp --reflink=auto "$original_tarball" "$rebuild/"
cp -a --reflink=auto "${linux_trees[0]}" "$rebuild/"
rebuilt_tree="$rebuild/$(basename "${linux_trees[0]}")"
(
	cd "$rebuild"
	dpkg-source --build "$(basename "$rebuilt_tree")"
) >"$evidence/source-rebuild.log" 2>&1
rebuilt_dsc="$(find "$rebuild" -maxdepth 1 -type f -name 'dkc-linux_*.dsc')"
test -n "$rebuilt_dsc"
dpkg-source -x "$rebuilt_dsc" "$rebuild/reconstructed" \
	>>"$evidence/source-rebuild.log" 2>&1
PYTHONPATH="$repo_root" python3 - "${linux_trees[0]}" "$rebuild/reconstructed" \
	"$evidence/source-rebuild.json" <<'PY'
import hashlib
import pathlib
import sys

from dkc.serialize import dumps
from dkc.sourcepackage import build_tree_manifest

original = build_tree_manifest(pathlib.Path(sys.argv[1]))
rebuilt = build_tree_manifest(pathlib.Path(sys.argv[2]))
if original != rebuilt:
    raise SystemExit("source-only rebuild changed the reconstructed source tree")
payload = original.encode()
pathlib.Path(sys.argv[3]).write_text(
    dumps(
        {
            "source_tree_entries": len(original.splitlines()),
            "source_tree_manifest_sha256": hashlib.sha256(payload).hexdigest(),
            "status": "PASS",
        }
    ),
    encoding="utf-8",
)
PY

mkdir "$evidence/download"
chmod 0777 "$evidence/download"
(
	cd "$evidence/download"
	apt-get download dkc-archive-keyring >../apt-download-keyring.log 2>&1
)
downloaded="$(find "$evidence/download" -maxdepth 1 -type f -name 'dkc-archive-keyring_*_all.deb')"
test -n "$downloaded"
cmp "$keyring" \
	<(
		dpkg-deb --fsys-tarfile "$downloaded" |
			tar -xOf - ./usr/share/keyrings/dkc-archive-keyring.gpg
	)
apt-get install -y --no-install-recommends dkc-archive-keyring \
	>"$evidence/apt-install-keyring.log" 2>&1
installed_keyring=/usr/share/keyrings/dkc-archive-keyring.gpg
cmp "$keyring" "$installed_keyring"
cmp "$repository/keys/archive-primary.fingerprint" \
	/usr/share/dkc-archive-keyring/archive-primary.fingerprint
cmp "$repository/keys/archive-signing-subkeys.fingerprints" \
	/usr/share/dkc-archive-keyring/archive-signing-subkeys.fingerprints
sed -i "s#Signed-By: ${keyring}#Signed-By: ${installed_keyring}#" \
	/etc/apt/sources.list.d/dkc.sources
apt-get update >"$evidence/apt-update-installed-keyring.log" 2>&1

negative_root="$(mktemp -d "$work_base/signed-negative-XXXXXX")"
mkdir -p "$negative_root/repository/keys"
cp -a "$repository/dists" "$negative_root/repository/"
cp "$keyring" "$negative_root/repository/keys/"
sed -i '0,/Origin: DKC/s//Origin: DKX/' \
	"$negative_root/repository/dists/trixie/InRelease"
sed "s#file:${repository}#file:${negative_root}/repository#" \
	/etc/apt/sources.list.d/dkc.sources >"$negative_root/corrupt.sources"
mkdir -p "$negative_root/lists-corrupt/partial" "$negative_root/lists-unsigned/partial"
chmod 0755 "$negative_root" "$negative_root/repository"
chown -R _apt "$negative_root/lists-corrupt" "$negative_root/lists-unsigned"
if apt-get update \
	-o Dir::Etc::sourcelist="$negative_root/corrupt.sources" \
	-o Dir::Etc::sourceparts=- \
	-o Dir::State::lists="$negative_root/lists-corrupt" \
	>"$evidence/corrupt-signature.log" 2>&1; then
	printf 'apt accepted a corrupted InRelease\n' >&2
	exit 1
fi
rm -f "$negative_root/repository/dists/trixie/InRelease" \
	"$negative_root/repository/dists/trixie/Release.gpg"
if apt-get update \
	-o Dir::Etc::sourcelist="$negative_root/corrupt.sources" \
	-o Dir::Etc::sourceparts=- \
	-o Dir::State::lists="$negative_root/lists-unsigned" \
	>"$evidence/missing-signature.log" 2>&1; then
	printf 'apt accepted unsigned Release metadata\n' >&2
	exit 1
fi

cat >"$evidence/result.env" <<EOF
status=PASS
apt_secure=PASS
by_hash=PASS
deb_src=PASS
source_only_rebuild=PASS
archive_key_inventory=PASS
keyring_install=PASS
release_kernel_install=PASS
current_version_selection=PASS
corrupt_signature_rejected=PASS
missing_signature_rejected=PASS
EOF

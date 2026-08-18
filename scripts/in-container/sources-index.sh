#!/usr/bin/env bash
# Print the `Sources` stanzas of one source package from one Debian suite.
#
# Runs INSIDE the confined container, as an unprivileged user with no
# capabilities. That constraint is deliberate rather than incidental: source
# discovery must never need root, so APT is pointed at a private state tree
# under the container's work area instead of /var/lib/apt.
#
# Only a `deb-src` entry is configured. No binary index for the discovery suite
# exists in this container, so no binary from it can enter a dependency closure.
#
# Usage: sources-index.sh <suite> <package> [--raw]
#   --raw  keep every field; by default only the parsed fields are kept, because
#          Binary and Build-Depends alone are megabytes per stanza.

set -Eeuo pipefail

SUITE="${1:?suite required}"
PACKAGE="${2:?package required}"
RAW="${3:-}"

MIRROR="${DKC_DEBIAN_MIRROR:-http://deb.debian.org/debian}"
KEYRING="${DKC_DEBIAN_KEYRING:-/usr/share/keyrings/debian-archive-keyring.gpg}"

[ -r "$KEYRING" ] || {
	echo "missing Debian archive keyring at ${KEYRING}" >&2
	exit 1
}

APT="$(mktemp -d "${TMPDIR:-/tmp}/dkc-apt-XXXXXX")"
trap 'rm -rf -- "$APT"' EXIT

mkdir -p \
	"$APT/etc/apt/sources.list.d" \
	"$APT/etc/apt/preferences.d" \
	"$APT/var/lib/apt/lists/partial" \
	"$APT/var/lib/dpkg" \
	"$APT/var/cache/apt/archives/partial" \
	"$APT/var/log/apt"
: >"$APT/var/lib/dpkg/status"

cat >"$APT/etc/apt/sources.list.d/discovery.sources" <<EOF
Types: deb-src
URIs: ${MIRROR}
Suites: ${SUITE}
Components: main
Signed-By: ${KEYRING}
EOF

# Every path APT would otherwise take from the system is redirected, so this
# leaves no trace outside the temporary tree and needs no privilege.
apt_opts=(
	-o "Dir::Etc=${APT}/etc/apt"
	-o "Dir::Etc::sourcelist="
	-o "Dir::Etc::sourceparts=${APT}/etc/apt/sources.list.d"
	-o "Dir::Etc::preferencesparts=${APT}/etc/apt/preferences.d"
	-o "Dir::Etc::trustedparts=/usr/share/keyrings"
	-o "Dir::State=${APT}/var/lib/apt"
	-o "Dir::State::status=${APT}/var/lib/dpkg/status"
	-o "Dir::Cache=${APT}/var/cache/apt"
	-o "Dir::Cache::archives=${APT}/var/cache/apt/archives"
	-o "Dir::Log=${APT}/var/log/apt"
	-o "Acquire::Languages=none"
)

apt-get "${apt_opts[@]}" update -qq >&2

# Record the authenticated Release identity alongside the stanzas: a version is
# only meaningful together with the signed metadata it came from.
release="$(find "$APT/var/lib/apt/lists" -name '*_InRelease' -print -quit)"
if [ -n "$release" ]; then
	sed -n '/^Origin:/p;/^Label:/p;/^Suite:/p;/^Codename:/p;/^Date:/p;/^Valid-Until:/p;/^Acquire-By-Hash:/p' \
		"$release" | sed 's/^/# release: /'
fi

# shellcheck disable=SC2016  # $(FILENAME) is an apt format placeholder, not a
# shell substitution.
index="$(apt-get "${apt_opts[@]}" indextargets --format '$(FILENAME)' 'Created-By: Sources' | head -1)"
[ -n "$index" ] || {
	echo "no Sources index for suite ${SUITE}" >&2
	exit 1
}

# apt-helper decompresses whatever format APT chose to store; never read the
# compressed file directly, because that format is APT's private choice.
extract() {
	/usr/lib/apt/apt-helper cat-file "$index" |
		awk -v RS= -v ORS='\n\n' -v pkg="$PACKAGE" '$0 ~ "^Package: " pkg "\n"'
}

if [ "$RAW" = "--raw" ]; then
	extract
else
	extract | awk '
		/^[A-Za-z0-9-]+:/ {
			field = $1
			sub(":", "", field)
			keep = (field == "Package" || field == "Version" || field == "Directory" ||
				field == "Format" || field == "Architecture" ||
				field == "Checksums-Sha256")
		}
		/^$/ { print ""; next }
		keep { print }
	'
fi

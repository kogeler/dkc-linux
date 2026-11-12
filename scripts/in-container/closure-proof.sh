#!/usr/bin/env bash
# Prove that no Sid or third-party binary is in the build closure.
#
# Runs inside the build container. The point is to resolve every installed
# package to the repository it actually came from, because reading the source
# lists proves only what was configured, not what was installed. A package
# pulled in before a list was edited, or installed from a local file, would be
# invisible to a text scan and perfectly visible here.
#
# Allowed origins are Debian's own suites for this release. A package that is
# present only in dpkg status is not attributable merely because the base image
# happened to contain it: the release lock must identify the exact repository
# bytes for every package in the closure.
#
# Needs network, because attribution requires current repository metadata.

set -Eeuo pipefail

ALLOWED_SUITES="${DKC_ALLOWED_SUITES:-trixie trixie-updates trixie-backports trixie-security}"
ALLOWED_HOSTS="${DKC_ALLOWED_HOSTS:-deb.debian.org security.debian.org}"

echo "allowed suites: ${ALLOWED_SUITES}"
echo "allowed hosts:  ${ALLOWED_HOSTS}"
echo

# --------------------------------------------------------------------------
# Configured repositories, parsed rather than grepped
# --------------------------------------------------------------------------

echo "=== configured binary repositories ==="
PYTHONPATH=/work/src python3 -m dkc.cli.closure_proof repositories "$ALLOWED_SUITES"
echo

# --------------------------------------------------------------------------
# Attribution of every installed package
# --------------------------------------------------------------------------

# The container root filesystem is read only, so APT gets a private state
# tree under the work area. The system sources and the real dpkg status are
# still what gets read: attribution must describe this image, not a
# synthetic one.
APT_STATE="$(mktemp -d /work/aptstate-XXXXXX)"
trap 'rm -rf -- "$APT_STATE"' EXIT
mkdir -p "$APT_STATE/lists/partial" "$APT_STATE/cache/archives/partial" "$APT_STATE/log"

apt_opts=(
	-o "Dir::State::lists=${APT_STATE}/lists"
	-o "Dir::State::status=/var/lib/dpkg/status"
	-o "Dir::Cache=${APT_STATE}/cache"
	-o "Dir::Cache::archives=${APT_STATE}/cache/archives"
	-o "Dir::Log=${APT_STATE}/log"
	-o "Acquire::Languages=none"
)

apt-get "${apt_opts[@]}" update -qq

echo "=== attributing every installed package to an origin ==="
APT_OPTS="${apt_opts[*]}" PYTHONPATH=/work/src python3 -m dkc.cli.closure_proof origins "$ALLOWED_SUITES" "$ALLOWED_HOSTS"

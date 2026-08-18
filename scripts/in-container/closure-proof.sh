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
python3 - "$ALLOWED_SUITES" <<'PY'
import glob
import sys

allowed = set(sys.argv[1].split())
problems = []

def paragraphs(text):
    for block in text.split("\n\n"):
        fields = {}
        key = None
        for line in block.splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            if line[0] in " \t" and key:
                fields[key] += " " + line.strip()
                continue
            name, _, value = line.partition(":")
            key = name.strip()
            fields[key] = value.strip()
        if fields:
            yield fields

for path in sorted(glob.glob("/etc/apt/sources.list.d/*.sources")):
    for fields in paragraphs(open(path).read()):
        types = fields.get("Types", "").split()
        suites = fields.get("Suites", "").split()
        uris = fields.get("URIs", "")
        signed = fields.get("Signed-By", "")
        kind = "binary" if "deb" in types else "source-only"
        print(f"  {path}: {kind} {uris} [{' '.join(suites)}] signed-by={signed or 'NONE'}")
        if "deb" in types:
            for suite in suites:
                if suite not in allowed:
                    problems.append(f"{path}: binary suite {suite!r} is not allowed")
            if not signed:
                problems.append(f"{path}: binary repository without Signed-By")

for path in sorted(glob.glob("/etc/apt/sources.list.d/*.list")) + ["/etc/apt/sources.list"]:
    try:
        text = open(path).read()
    except FileNotFoundError:
        continue
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            print(f"  {path}: one-line entry {line}")
            if line.startswith("deb "):
                problems.append(f"{path}: one-line binary entry, expected Deb822")

if problems:
    print()
    for problem in problems:
        print(f"  PROBLEM: {problem}")
    sys.exit(1)
PY
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
APT_OPTS="${apt_opts[*]}" python3 - "$ALLOWED_SUITES" "$ALLOWED_HOSTS" <<'PY'
import re
import subprocess
import sys

allowed_suites = set(sys.argv[1].split())
allowed_hosts = set(sys.argv[2].split())

# ${Package}, not ${binary:Package}: the latter carries an architecture
# qualifier that apt-cache policy does not use in its stanza headers, and the
# mismatch would silently leave every multi-arch package unattributed.
installed = subprocess.run(
    ["dpkg-query", "-W", "-f", "${Package}\t${Version}\n"],
    capture_output=True, text=True, check=True,
).stdout.splitlines()

packages = {}
for line in installed:
    if not line.strip():
        continue
    name, _, version = line.partition("\t")
    packages[name] = version

import os
policy = subprocess.run(
    ["apt-cache", *os.environ["APT_OPTS"].split(), "policy", *packages],
    capture_output=True, text=True, check=True,
).stdout

# apt-cache policy prints one stanza per package; the installed version is the
# line marked with ***, and its origin follows on the next line.
current = None
attribution = {}
lines = policy.splitlines()
for index, line in enumerate(lines):
    if not line.startswith(" ") and line.endswith(":"):
        current = line[:-1].split(":")[0]
        continue
    if current and line.lstrip().startswith("***"):
        for follow in lines[index + 1:index + 4]:
            match = re.search(r"(https?)://([^/ ]+)(\S*)\s+(\S+)/(\S+)", follow)
            if match:
                attribution[current] = (match.group(2), match.group(4))
                break
            if "/var/lib/dpkg/status" in follow:
                attribution[current] = ("dpkg-status", "installed-only")
                break

unattributed = []
rejected = []
counts: dict[str, int] = {}

for name in sorted(packages):
    host, suite = attribution.get(name, (None, None))
    if host is None:
        unattributed.append(name)
        continue
    if host == "dpkg-status":
        unattributed.append(name)
        continue
    key = f"{host} {suite}"
    counts[key] = counts.get(key, 0) + 1
    if host not in allowed_hosts or suite.split("/")[0] not in allowed_suites:
        rejected.append(f"{name}: {host} {suite}")

for key in sorted(counts):
    print(f"  {counts[key]:4d}  {key}")

print(f"\n  total installed: {len(packages)}")
print(f"  attributed:      {sum(counts.values())}")
print(f"  unattributed:    {len(unattributed)}")

if rejected:
    print("\n  REJECTED, outside the allowed origins:")
    for item in rejected:
        print(f"    {item}")
    sys.exit(1)

if unattributed:
    print("\n  not attributable to a configured repository "
          "(installed version superseded upstream):")
    for name in unattributed[:20]:
        print(f"    {name} {packages[name]}")
    if len(unattributed) > 20:
        print(f"    ... and {len(unattributed) - 20} more")
    sys.exit(1)

print("\nRESULT: PASS, no package resolves to a Sid or third-party origin")
PY

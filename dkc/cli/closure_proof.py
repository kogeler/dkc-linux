"""Build-closure origin checks for closure-proof.sh."""

from __future__ import annotations

import glob
import hashlib
import os
import pathlib
import re
import subprocess
import sys
from collections.abc import Iterator


def check_repositories(arguments: list[str]) -> None:
    """Require every binary repository to be an allowed, keyed Debian suite."""
    allowed = set(arguments[0].split())
    problems: list[str] = []

    def paragraphs(text: str) -> Iterator[dict[str, str]]:
        for block in text.split("\n\n"):
            fields: dict[str, str] = {}
            key: str | None = None
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
        for fields in paragraphs(pathlib.Path(path).read_text()):
            types = fields.get("Types", "").split()
            suites = fields.get("Suites", "").split()
            uris = fields.get("URIs", "")
            signed = fields.get("Signed-By", "")
            kind = "binary" if "deb" in types else "source-only"
            print(
                f"  {path}: {kind} {uris} [{' '.join(suites)}] "
                f"signed-by={signed or 'NONE'}"
            )
            if "deb" in types:
                for suite in suites:
                    if suite not in allowed:
                        problems.append(
                            f"{path}: binary suite {suite!r} is not allowed"
                        )
                if not signed:
                    problems.append(f"{path}: binary repository without Signed-By")

    for path in sorted(glob.glob("/etc/apt/sources.list.d/*.list")) + [
        "/etc/apt/sources.list"
    ]:
        try:
            text = pathlib.Path(path).read_text()
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


def check_origins(arguments: list[str]) -> None:
    """Attribute every installed package to an allowed Debian origin."""
    allowed_suites = set(arguments[0].split())
    allowed_hosts = set(arguments[1].split())

    # ${Package}, not ${binary:Package}: the latter carries an architecture
    # qualifier that apt-cache policy does not use in its stanza headers, and the
    # mismatch would silently leave every multi-arch package unattributed.
    installed = subprocess.run(
        ["dpkg-query", "-W", "-f", "${Package}\t${Version}\n"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()

    packages = {}
    for line in installed:
        if not line.strip():
            continue
        name, _, version = line.partition("\t")
        packages[name] = version

    policy = subprocess.run(
        ["apt-cache", *os.environ["APT_OPTS"].split(), "policy", *packages],
        capture_output=True,
        text=True,
        check=True,
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
            for follow in lines[index + 1 : index + 4]:
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

    # Packages rebuilt from the patched-package ledger are attributed to the
    # image's own manifest, but only for the exact installed version and only when
    # the retained archive still has the recorded SHA-256.

    patched = {}
    try:
        with open("/usr/share/dkc/patched-packages.tsv", encoding="utf-8") as stream:
            rows = [line.rstrip("\n").split("\t") for line in stream if line.strip()]
    except FileNotFoundError:
        rows = [[]]
    for row in rows[1:]:
        fix, source, debian_version, package, version, architecture, sha256 = row
        matches = []
        for archive in glob.glob(
            f"/var/cache/apt/archives/{package}_*_{architecture}.deb"
        ):
            with open(archive, "rb") as stream:
                if hashlib.sha256(stream.read()).hexdigest() == sha256:
                    matches.append(archive)
        if len(matches) != 1:
            rejected.append(
                f"{package}: patched archive with the recorded SHA-256 is missing"
            )
            continue
        patched[package] = (version, f"dkc-patched {source} {debian_version}")

    for name in sorted(packages):
        if name in patched and packages[name] == patched[name][0]:
            key = patched[name][1]
            counts[key] = counts.get(key, 0) + 1
            continue
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
        print(
            "\n  not attributable to a configured repository "
            "(installed version superseded upstream):"
        )
        for name in unattributed[:20]:
            print(f"    {name} {packages[name]}")
        if len(unattributed) > 20:
            print(f"    ... and {len(unattributed) - 20} more")
        sys.exit(1)

    print("\nRESULT: PASS, no package resolves to a Sid or third-party origin")


COMMANDS = {
    "repositories": check_repositories,
    "origins": check_origins,
}


def main() -> None:
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        commands = "|".join(COMMANDS)
        raise SystemExit(f"usage: python3 -m dkc.cli.closure_proof {{{commands}}} ...")
    COMMANDS[sys.argv[1]](sys.argv[2:])


if __name__ == "__main__":
    main()

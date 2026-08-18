#!/usr/bin/env python3
"""Lock Debian archives retained by an immutable build environment."""

from __future__ import annotations

import argparse
import hashlib
import pathlib
import re
import subprocess
from collections.abc import Callable

PackageIdentity = tuple[str, str, str]
LockRow = tuple[str, str, str, str, str, str]
IdentityReader = Callable[[pathlib.Path], PackageIdentity]
_ARCHIVE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+_~%:-]*\.deb$")


def read_installed(path: pathlib.Path) -> set[PackageIdentity]:
    records: set[PackageIdentity] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        fields = tuple(line.split("\t"))
        if len(fields) != 3 or not all(fields):
            raise ValueError("installed package inventory is malformed")
        record = (fields[0], fields[1], fields[2])
        if record in records:
            raise ValueError("installed package inventory contains a duplicate")
        records.add(record)
    if not records:
        raise ValueError("installed package inventory is empty")
    return records


def read_deb_identity(path: pathlib.Path) -> PackageIdentity:
    fields = subprocess.check_output(
        [
            "dpkg-deb",
            "--showformat=${binary:Package}\t${Version}\t${Architecture}",
            "--show",
            path,
        ],
        text=True,
    ).split("\t")
    if len(fields) != 3 or not all(fields):
        raise ValueError("cached Debian archive identity is malformed")
    return fields[0], fields[1], fields[2]


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def retained_archive_rows(
    installed: set[PackageIdentity],
    archive_root: pathlib.Path,
    *,
    identity_reader: IdentityReader = read_deb_identity,
) -> tuple[LockRow, ...]:
    rows: dict[PackageIdentity, LockRow] = {}
    for archive in sorted(archive_root.glob("*.deb")):
        if archive.is_symlink() or not archive.is_file() or not _ARCHIVE_RE.fullmatch(
            archive.name
        ):
            raise ValueError("build environment contains an unsafe Debian archive")
        identity = identity_reader(archive)
        if identity not in installed:
            continue
        if identity in rows:
            raise ValueError("build environment contains a duplicate installed archive")
        package, version, architecture = identity
        rows[identity] = (
            package,
            version,
            architecture,
            f"image-cache://{archive.name}",
            str(archive.stat().st_size),
            sha256_file(archive),
        )
    if not rows:
        raise ValueError("build environment contains no retained Debian archives")
    return tuple(sorted(rows.values()))


def write_lock(path: pathlib.Path, rows: tuple[LockRow, ...]) -> None:
    with path.open("w", encoding="utf-8") as stream:
        stream.write("package\tversion\tarchitecture\turi\tsize\tsha256\n")
        for row in rows:
            stream.write("\t".join(row) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--packages", type=pathlib.Path, required=True)
    parser.add_argument("--archives", type=pathlib.Path, required=True)
    parser.add_argument("--output", type=pathlib.Path, required=True)
    args = parser.parse_args()
    rows = retained_archive_rows(read_installed(args.packages), args.archives)
    write_lock(args.output, rows)
    print(f"toolchain lock PASS: {len(rows)} image-cached .deb files hashed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

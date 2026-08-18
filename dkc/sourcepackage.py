"""Validation primitives for a complete Debian source-package bundle.

The archive publisher must never infer that a directory containing a ``.dsc``
is complete.  This module validates the exact five-file source deliverable,
the cross-references in its Deb822 control files, and a content manifest of the
source tree reconstructed by ``dpkg-source``.
"""

from __future__ import annotations

import hashlib
import os
import pathlib
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

__all__ = [
    "BundleFile",
    "SourceBundle",
    "build_tree_manifest",
    "parse_checksums_sha256",
    "parse_deb822",
    "validate_source_bundle",
]


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_FILENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+~_-]*$")


def parse_deb822(text: str) -> dict[str, str]:
    """Parse one strict Deb822 paragraph without accepting duplicate fields."""

    fields: dict[str, str] = {}
    current: str | None = None
    for number, line in enumerate(text.splitlines(), start=1):
        if not line:
            raise ValueError(f"unexpected paragraph boundary on line {number}")
        if line[:1] in (" ", "\t"):
            if current is None:
                raise ValueError(f"continuation without a field on line {number}")
            fields[current] += "\n" + line[1:]
            continue
        key, separator, value = line.partition(":")
        if not separator or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*", key):
            raise ValueError(f"malformed Deb822 field on line {number}")
        if key in fields:
            raise ValueError(f"duplicate Deb822 field: {key}")
        current = key
        fields[key] = value.lstrip()
    if not fields:
        raise ValueError("empty Deb822 record")
    return fields


@dataclass(frozen=True)
class BundleFile:
    name: str
    size: int
    sha256: str

    def __post_init__(self) -> None:
        if not _SAFE_FILENAME_RE.fullmatch(self.name):
            raise ValueError(f"unsafe source-bundle filename: {self.name!r}")
        if self.size < 0:
            raise ValueError("source-bundle size must not be negative")
        if not _SHA256_RE.fullmatch(self.sha256):
            raise ValueError("source-bundle digest must be lowercase SHA-256")

    def to_dict(self) -> dict[str, object]:
        return {"name": self.name, "sha256": self.sha256, "size": self.size}


def parse_checksums_sha256(value: str, field_name: str) -> dict[str, BundleFile]:
    """Parse a ``Checksums-Sha256`` value and reject paths and duplicates."""

    records: dict[str, BundleFile] = {}
    for line in value.splitlines():
        if not line.strip():
            continue
        parts = line.split()
        if len(parts) != 3:
            raise ValueError(f"malformed {field_name} checksum line: {line!r}")
        digest, size_text, name = parts
        if not size_text.isdecimal():
            raise ValueError(f"malformed {field_name} size: {size_text!r}")
        record = BundleFile(name=name, size=int(size_text), sha256=digest)
        if name in records:
            raise ValueError(f"duplicate {field_name} member: {name}")
        records[name] = record
    if not records:
        raise ValueError(f"empty {field_name} checksum field")
    return records


def _sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _actual(root: pathlib.Path, names: Iterable[str]) -> dict[str, BundleFile]:
    result: dict[str, BundleFile] = {}
    for name in names:
        path = root / name
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"source-bundle member is not a plain file: {name}")
        result[name] = BundleFile(name, path.stat().st_size, _sha256(path))
    return result


def _require_records(
    actual: Mapping[str, BundleFile],
    declared: Mapping[str, BundleFile],
    label: str,
) -> None:
    if set(actual) != set(declared):
        raise ValueError(
            f"{label} member set differs: "
            f"missing={sorted(set(actual) - set(declared))}, "
            f"unexpected={sorted(set(declared) - set(actual))}"
        )
    for name, expected in actual.items():
        if declared[name] != expected:
            raise ValueError(f"{label} size or digest differs for {name}")


@dataclass(frozen=True)
class SourceBundle:
    package: str
    version: str
    files: tuple[BundleFile, ...]
    dsc: str
    orig: str
    debian: str
    changes: str
    buildinfo: str
    schema_version: int = 1

    def to_dict(self) -> dict[str, object]:
        return {
            "buildinfo": self.buildinfo,
            "changes": self.changes,
            "debian": self.debian,
            "dsc": self.dsc,
            "files": [item.to_dict() for item in self.files],
            "orig": self.orig,
            "package": self.package,
            "schema_version": self.schema_version,
            "version": self.version,
        }

def validate_source_bundle(
    root: pathlib.Path,
    *,
    package: str,
    version: str,
    upstream_version: str,
    expected_binary_packages: Iterable[str],
) -> SourceBundle:
    """Validate a source-only upload and every hash edge inside it."""

    version_filename = version.split(":", maxsplit=1)[-1]
    expected_names = {
        "dsc": f"{package}_{version_filename}.dsc",
        "orig": f"{package}_{upstream_version}.orig.tar.xz",
        "debian": f"{package}_{version_filename}.debian.tar.xz",
        "changes": f"{package}_{version_filename}_source.changes",
        "buildinfo": f"{package}_{version_filename}_source.buildinfo",
    }
    found = {
        path.name
        for path in root.iterdir()
        if path.is_file() or path.is_symlink()
    }
    if found != set(expected_names.values()):
        raise ValueError(
            "source bundle is not the exact five-file deliverable: "
            f"missing={sorted(set(expected_names.values()) - found)}, "
            f"unexpected={sorted(found - set(expected_names.values()))}"
        )
    actual = _actual(root, expected_names.values())

    dsc_fields = parse_deb822((root / expected_names["dsc"]).read_text(encoding="utf-8"))
    for key, expected in (("Format", "3.0 (quilt)"), ("Source", package), ("Version", version)):
        if dsc_fields.get(key) != expected:
            raise ValueError(f".dsc {key}={dsc_fields.get(key)!r}, expected {expected!r}")
    dsc_members = parse_checksums_sha256(
        dsc_fields.get("Checksums-Sha256", ""), ".dsc"
    )
    _require_records(
        {name: actual[name] for name in (expected_names["orig"], expected_names["debian"])},
        dsc_members,
        ".dsc",
    )
    binaries = {
        item.strip()
        for item in dsc_fields.get("Binary", "").replace("\n", " ").split(",")
        if item.strip()
    }
    expected_binaries = set(expected_binary_packages)
    if not expected_binaries or binaries != expected_binaries:
        raise ValueError(".dsc Binary does not describe the exact publication package graph")

    buildinfo_fields = parse_deb822(
        (root / expected_names["buildinfo"]).read_text(encoding="utf-8")
    )
    for key, expected in (("Source", package), ("Version", version), ("Architecture", "source")):
        if buildinfo_fields.get(key) != expected:
            raise ValueError(
                f"source .buildinfo {key}={buildinfo_fields.get(key)!r}, expected {expected!r}"
            )
    buildinfo_members = parse_checksums_sha256(
        buildinfo_fields.get("Checksums-Sha256", ""), "source .buildinfo"
    )
    _require_records(
        {expected_names["dsc"]: actual[expected_names["dsc"]]},
        buildinfo_members,
        "source .buildinfo",
    )

    changes_fields = parse_deb822(
        (root / expected_names["changes"]).read_text(encoding="utf-8")
    )
    for key, expected in (
        ("Source", package),
        ("Version", version),
        ("Architecture", "source"),
        ("Distribution", "trixie"),
    ):
        if changes_fields.get(key) != expected:
            raise ValueError(
                f"source .changes {key}={changes_fields.get(key)!r}, expected {expected!r}"
            )
    changes_members = parse_checksums_sha256(
        changes_fields.get("Checksums-Sha256", ""), "source .changes"
    )
    _require_records(
        {
            name: actual[name]
            for name in (
                expected_names["dsc"],
                expected_names["orig"],
                expected_names["debian"],
                expected_names["buildinfo"],
            )
        },
        changes_members,
        "source .changes",
    )

    files = tuple(actual[name] for name in sorted(actual))
    return SourceBundle(
        package=package,
        version=version,
        files=files,
        dsc=expected_names["dsc"],
        orig=expected_names["orig"],
        debian=expected_names["debian"],
        changes=expected_names["changes"],
        buildinfo=expected_names["buildinfo"],
    )


def build_tree_manifest(root: pathlib.Path) -> str:
    """Return a deterministic content/mode manifest for a clean source tree."""

    if not root.is_dir() or root.is_symlink():
        raise ValueError("source-tree root must be a plain directory")
    records: list[str] = []
    for directory, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        directory_path = pathlib.Path(directory)
        relative_directory = directory_path.relative_to(root)
        dirnames[:] = sorted(
            name
            for name in dirnames
            if not (relative_directory == pathlib.Path(".") and name == ".pc")
        )
        for name in sorted(filenames):
            path = directory_path / name
            relative = path.relative_to(root).as_posix()
            stat_result = path.lstat()
            mode = stat_result.st_mode & 0o7777
            if path.is_symlink():
                target = os.readlink(path)
                records.append(f"l\t{mode:04o}\t{relative}\t{target}")
            elif path.is_file():
                records.append(
                    f"f\t{mode:04o}\t{stat_result.st_size}\t{_sha256(path)}\t{relative}"
                )
            else:
                raise ValueError(f"special file in source tree: {relative}")
    return "\n".join(records) + "\n"

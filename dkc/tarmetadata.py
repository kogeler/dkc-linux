"""Deterministic filesystem and tar metadata for reproducible build outputs."""

from __future__ import annotations

import os
import pathlib
import stat
import tarfile
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import BinaryIO

__all__ = [
    "TarMetadata",
    "normalize_tree_metadata",
    "publication_epoch_after_source",
    "require_package_archive_evidence",
    "require_epoch_not_future",
    "require_source_archive_evidence",
    "validate_tar_path",
    "validate_tar_stream",
]


@dataclass(frozen=True)
class TarMetadata:
    """Bounded evidence about metadata inspected in one complete tar stream."""

    member_count: int
    minimum_mtime: int
    maximum_mtime: int

    def to_dict(self) -> dict[str, int]:
        return {
            "member_count": self.member_count,
            "minimum_mtime": self.minimum_mtime,
            "maximum_mtime": self.maximum_mtime,
        }


def _epoch(value: object, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(f"{label} must be a positive integer epoch")
    return value


def publication_epoch_after_source(source_epoch: int) -> int:
    """Place the downstream changelog one second after its source entry."""

    return _epoch(source_epoch, "source epoch") + 1


def require_epoch_not_future(epoch: int, current_epoch: int, label: str) -> None:
    """Reject an epoch that would make clamp-only archive tools retain wall time."""

    checked = _epoch(epoch, label)
    now = _epoch(current_epoch, "current epoch")
    if checked > now:
        raise ValueError(
            f"{label} {checked} is ahead of the build clock {now}; "
            "retry after the deterministic epoch"
        )


def _metadata_record(value: object, epoch: int, label: str) -> TarMetadata:
    if not isinstance(value, Mapping) or set(value) != {
        "maximum_mtime",
        "member_count",
        "minimum_mtime",
    }:
        raise ValueError(f"{label} is malformed")
    minimum = value.get("minimum_mtime")
    maximum = value.get("maximum_mtime")
    count = value.get("member_count")
    if (
        not isinstance(minimum, int)
        or isinstance(minimum, bool)
        or not isinstance(maximum, int)
        or isinstance(maximum, bool)
        or not isinstance(count, int)
        or isinstance(count, bool)
        or minimum < 0
        or maximum < minimum
        or maximum > epoch
        or count < 1
    ):
        raise ValueError(f"{label} is outside deterministic metadata policy")
    if minimum != epoch or maximum != epoch:
        raise ValueError(f"{label} does not use the exact publication epoch")
    return TarMetadata(count, minimum, maximum)


def require_source_archive_evidence(
    value: object, epoch: object, label: str
) -> TarMetadata:
    """Validate the complete exact-mtime evidence for one Debian source tar."""

    checked_epoch = _epoch(epoch, "source archive evidence epoch")
    if not isinstance(value, Mapping) or set(value) != {
        "epoch",
        "maximum_mtime",
        "member_count",
        "minimum_mtime",
        "status",
    }:
        raise ValueError(f"{label} is malformed")
    if value.get("status") != "PASS" or value.get("epoch") != checked_epoch:
        raise ValueError(f"{label} does not match the publication epoch")
    return _metadata_record(
        {
            key: value[key]
            for key in ("maximum_mtime", "member_count", "minimum_mtime")
        },
        checked_epoch,
        label,
    )


def require_package_archive_evidence(
    value: object,
    package_names: Collection[str],
    epoch: object,
    label: str,
) -> None:
    """Validate exact metadata evidence for both tar streams of every .deb."""

    checked_epoch = _epoch(epoch, "package archive evidence epoch")
    expected = set(package_names)
    if (
        not expected
        or len(expected) != len(package_names)
        or any(not isinstance(name, str) or not name for name in expected)
    ):
        raise ValueError(f"{label} has an invalid expected package set")
    if not isinstance(value, Mapping) or set(value) != {"epoch", "packages", "status"}:
        raise ValueError(f"{label} is malformed")
    if value.get("status") != "PASS" or value.get("epoch") != checked_epoch:
        raise ValueError(f"{label} does not match the publication epoch")
    packages = value.get("packages")
    if not isinstance(packages, Mapping) or set(packages) != expected:
        raise ValueError(f"{label} has a different package set")
    for package, record in packages.items():
        if not isinstance(record, Mapping) or set(record) != {"control", "data"}:
            raise ValueError(f"{label} is malformed for {package}")
        for archive_kind in ("control", "data"):
            _metadata_record(
                record[archive_kind],
                checked_epoch,
                f"{label} for {package} {archive_kind} tar",
            )


def _normalize_path(path: pathlib.Path, epoch: int) -> None:
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode):
        os.utime(path, (epoch, epoch), follow_symlinks=False)
        return
    if stat.S_ISDIR(metadata.st_mode):
        path.chmod(0o755)
    elif stat.S_ISREG(metadata.st_mode):
        path.chmod(0o755 if metadata.st_mode & 0o111 else 0o644)
    else:
        raise ValueError(f"metadata tree contains a special entry: {path}")
    os.utime(path, (epoch, epoch), follow_symlinks=False)


def normalize_tree_metadata(root: pathlib.Path, epoch: int) -> None:
    """Normalize every mode and timestamp without following a tree symlink."""

    normalized_epoch = _epoch(epoch, "normalization epoch")
    if not root.is_dir() or root.is_symlink():
        raise ValueError("metadata-tree root must be a plain directory")

    # Files and symlinked directories are handled before their real parent
    # directories. This prevents traversal itself from leaving a parent mtime
    # different from the requested deterministic value.
    for directory, dirnames, filenames in os.walk(
        root, topdown=False, followlinks=False
    ):
        directory_path = pathlib.Path(directory)
        for name in filenames:
            _normalize_path(directory_path / name, normalized_epoch)
        for name in dirnames:
            path = directory_path / name
            if path.is_symlink():
                _normalize_path(path, normalized_epoch)
        _normalize_path(directory_path, normalized_epoch)


def _canonical_member_name(name: str, label: str) -> str:
    if not name or "\x00" in name or "\\" in name:
        raise ValueError(f"{label} contains an unsafe tar member name: {name!r}")
    stripped = name
    while stripped.startswith("./"):
        stripped = stripped[2:]
    if stripped in ("", "."):
        return "."
    pure = pathlib.PurePosixPath(stripped)
    if pure.is_absolute() or ".." in pure.parts or pure.as_posix() != stripped:
        raise ValueError(f"{label} contains an unsafe tar member name: {name!r}")
    return stripped


def validate_tar_stream(
    stream: BinaryIO,
    *,
    label: str,
    epoch: int,
    exact_mtime: bool,
    expected_prefix: str | None = None,
    require_sorted: bool = False,
    normalized_modes: bool = False,
) -> TarMetadata:
    """Consume and validate one complete tar stream's nondeterministic fields."""

    checked_epoch = _epoch(epoch, "tar metadata epoch")
    prefix = (
        _canonical_member_name(expected_prefix, label)
        if expected_prefix is not None
        else None
    )
    if prefix == ".":
        raise ValueError("tar metadata prefix must name a directory")

    names: set[str] = set()
    previous_order_key: tuple[str, ...] | None = None
    minimum_mtime: int | None = None
    maximum_mtime: int | None = None
    count = 0
    try:
        with tarfile.open(fileobj=stream, mode="r|*") as archive:
            if archive.pax_headers:
                raise ValueError(f"{label} contains global PAX metadata")
            for member in archive:
                name = _canonical_member_name(member.name, label)
                if name in names:
                    raise ValueError(f"{label} contains a duplicate tar member: {name}")
                if prefix is not None and name != prefix and not name.startswith(
                    f"{prefix}/"
                ):
                    raise ValueError(
                        f"{label} member escapes the {prefix} tree: {member.name!r}"
                    )
                order_key = pathlib.PurePosixPath(name).parts
                if (
                    require_sorted
                    and previous_order_key is not None
                    and order_key <= previous_order_key
                ):
                    raise ValueError(
                        f"{label} tar members are not in deterministic tree order"
                    )
                previous_order_key = order_key
                names.add(name)

                if member.pax_headers:
                    raise ValueError(f"{label} member {name} contains PAX metadata")
                if member.uid != 0 or member.gid != 0:
                    raise ValueError(f"{label} member {name} does not have numeric root ownership")
                if member.uname not in ("", "root") or member.gname not in ("", "root"):
                    raise ValueError(f"{label} member {name} has unstable owner names")
                if not (
                    member.isreg()
                    or member.isdir()
                    or member.issym()
                    or member.islnk()
                ):
                    raise ValueError(f"{label} member {name} has a special tar type")
                if member.islnk():
                    _canonical_member_name(member.linkname, label)

                if int(member.mtime) != member.mtime:
                    raise ValueError(f"{label} member {name} has a fractional mtime")
                mtime = int(member.mtime)
                if exact_mtime and mtime != checked_epoch:
                    raise ValueError(
                        f"{label} member {name} mtime {mtime} differs from {checked_epoch}"
                    )
                if not exact_mtime and mtime > checked_epoch:
                    raise ValueError(
                        f"{label} member {name} mtime {mtime} exceeds {checked_epoch}"
                    )

                if normalized_modes:
                    mode = member.mode & 0o7777
                    if member.isdir() and mode != 0o755:
                        raise ValueError(f"{label} directory {name} mode is not 0755")
                    if member.isreg() and mode not in (0o644, 0o755):
                        raise ValueError(f"{label} file {name} has a non-public mode")
                    if member.issym() and mode != 0o777:
                        raise ValueError(f"{label} symlink {name} mode is not 0777")
                    if member.islnk():
                        raise ValueError(f"{label} unexpectedly stores a hard link: {name}")

                minimum_mtime = (
                    mtime if minimum_mtime is None else min(minimum_mtime, mtime)
                )
                maximum_mtime = (
                    mtime if maximum_mtime is None else max(maximum_mtime, mtime)
                )
                count += 1
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise ValueError(f"cannot read {label}: {exc}") from exc

    if count == 0 or minimum_mtime is None or maximum_mtime is None:
        raise ValueError(f"{label} is an empty tar archive")
    return TarMetadata(count, minimum_mtime, maximum_mtime)


def validate_tar_path(
    path: pathlib.Path,
    *,
    label: str,
    epoch: int,
    exact_mtime: bool,
    expected_prefix: str | None = None,
    require_sorted: bool = False,
    normalized_modes: bool = False,
) -> TarMetadata:
    """Validate metadata from one plain compressed or uncompressed tar file."""

    if not path.is_file() or path.is_symlink():
        raise ValueError(f"{label} is not a plain file: {path}")
    with path.open("rb") as stream:
        return validate_tar_stream(
            stream,
            label=label,
            epoch=epoch,
            exact_mtime=exact_mtime,
            expected_prefix=expected_prefix,
            require_sorted=require_sorted,
            normalized_modes=normalized_modes,
        )

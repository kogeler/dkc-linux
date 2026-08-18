"""Strict readers for hash-bound handoff directories."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

__all__ = ["verify_evidence_directory"]


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _safe_relative(value: str) -> Path:
    path = Path(value)
    if (
        not value
        or value != value.strip()
        or path.as_posix() != value
        or path.is_absolute()
        or "\\" in value
        or any(part in ("", ".", "..") for part in path.parts)
    ):
        raise ValueError("evidence inventory contains an unsafe path")
    return path


def verify_evidence_directory(root: Path) -> tuple[str, ...]:
    """Verify an exact ``evidence.sha256`` handoff and return its paths.

    The manifest must inventory every regular file below ``root`` except
    itself. Symlinks, special files, duplicates, missing files, and unlisted
    additions all fail closed.
    """

    if root.is_symlink():
        raise ValueError("evidence root is a symbolic link")
    resolved = root.resolve(strict=True)
    if not resolved.is_dir():
        raise ValueError("evidence root is not a directory")
    checksum = resolved / "evidence.sha256"
    if not checksum.is_file() or checksum.is_symlink():
        raise ValueError("evidence checksum manifest is not a regular file")

    expected: dict[str, str] = {}
    for line in checksum.read_text(encoding="utf-8").splitlines():
        digest, separator, raw_name = line.partition("  ")
        if not separator or not _SHA256_RE.fullmatch(digest):
            raise ValueError("evidence checksum manifest is malformed")
        name = _safe_relative(raw_name).as_posix()
        if name == "evidence.sha256" or name in expected:
            raise ValueError("evidence checksum manifest repeats or names itself")
        expected[name] = digest
    if not expected:
        raise ValueError("evidence checksum manifest is empty")

    observed: dict[str, Path] = {}
    for path in sorted(resolved.rglob("*")):
        if path.is_symlink():
            raise ValueError("evidence handoff contains a symbolic link")
        if path.is_dir():
            continue
        if not path.is_file():
            raise ValueError("evidence handoff contains a special file")
        name = path.relative_to(resolved).as_posix()
        if name != "evidence.sha256":
            observed[name] = path
    if set(observed) != set(expected):
        raise ValueError("evidence checksum manifest does not match the handoff")
    for name, path in observed.items():
        hasher = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                hasher.update(chunk)
        if hasher.hexdigest() != expected[name]:
            raise ValueError("evidence checksum verification failed")
    return tuple(sorted(expected))

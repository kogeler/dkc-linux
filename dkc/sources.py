"""Parsing of Debian `Sources` indexes and the source inventory they describe.

The sid `Sources` index holds every `src:linux` version whose binaries are still
in the archive: sixteen of them on 2026-08-10, with `6.12.94-1` first and
`7.1.7-1` newest. Selecting the first stanza, or the lexically largest version,
silently picks a two-year-old kernel. Selection therefore always goes through an
explicit Debian version comparison over every candidate.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .debver import DebianVersion, compare

__all__ = ["SourceFile", "SourceStanza", "parse_sources", "select_newest"]

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
# A member file name must be a plain path component: no traversal, no absolute
# path, no shell or control character.
_MEMBER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+~-]*$")
_DIRECTORY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+~/-]*$")


class MalformedIndex(ValueError):
    """The index is not a well-formed Debian control file."""


@dataclass(frozen=True)
class SourceFile:
    """One member of a source package, with the hash the index declares."""

    name: str
    size: int
    sha256: str

    def __post_init__(self) -> None:
        if not _MEMBER_RE.match(self.name):
            raise MalformedIndex(f"unsafe source member name: {self.name!r}")
        if self.size <= 0:
            raise MalformedIndex(f"non-positive size for {self.name}: {self.size}")
        if not _SHA256_RE.match(self.sha256):
            raise MalformedIndex(f"malformed SHA-256 for {self.name}: {self.sha256!r}")


@dataclass(frozen=True)
class SourceStanza:
    """One `Package`/`Version` stanza of a `Sources` index."""

    package: str
    version: DebianVersion
    directory: str
    files: tuple[SourceFile, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        dsc_count = sum(member.name.endswith(".dsc") for member in self.files)
        if dsc_count != 1:
            raise MalformedIndex(
                f"expected exactly one .dsc member for {self.package} "
                f"{self.version}, found {dsc_count}"
            )

    @property
    def dsc(self) -> SourceFile:
        for member in self.files:
            if member.name.endswith(".dsc"):
                return member
        raise MalformedIndex(f"no .dsc member for {self.package} {self.version}")

    def uri(self, base_url: str, member: SourceFile) -> str:
        return f"{base_url.rstrip('/')}/{self.directory.strip('/')}/{member.name}"


def _parse_paragraphs(text: str) -> list[dict[str, str]]:
    """Split a control file into paragraphs of folded fields.

    Continuation lines start with a space or tab and belong to the preceding
    field; that is how `Checksums-Sha256` carries one entry per line.
    """
    paragraphs: list[dict[str, str]] = []
    current: dict[str, str] = {}
    last_key: str | None = None

    for raw_line in text.splitlines():
        if not raw_line.strip():
            if current:
                paragraphs.append(current)
                current = {}
                last_key = None
            continue
        if raw_line[0] in " \t":
            if last_key is None:
                raise MalformedIndex(f"continuation line without a field: {raw_line!r}")
            current[last_key] += "\n" + raw_line.strip()
            continue
        if ":" not in raw_line:
            raise MalformedIndex(f"line is not a field: {raw_line!r}")
        key, _, value = raw_line.partition(":")
        key = key.strip()
        if not key:
            raise MalformedIndex(f"empty field name in {raw_line!r}")
        if key in current:
            raise MalformedIndex(f"duplicate field {key!r}")
        current[key] = value.strip()
        last_key = key

    if current:
        paragraphs.append(current)
    return paragraphs


def _parse_checksums(block: str) -> dict[str, SourceFile]:
    """Parse a `Checksums-Sha256` field: `<sha256> <size> <name>` per line."""
    members: dict[str, SourceFile] = {}
    for line in block.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) != 3:
            raise MalformedIndex(f"malformed checksum line: {line!r}")
        sha256, size, name = parts
        try:
            size_int = int(size)
        except ValueError as exc:
            raise MalformedIndex(f"non-integer size in {line!r}") from exc
        if name in members:
            raise MalformedIndex(f"duplicate source member: {name!r}")
        members[name] = SourceFile(name=name, size=size_int, sha256=sha256)
    if not members:
        raise MalformedIndex("empty Checksums-Sha256 field")
    return members


def parse_sources(text: str, package: str) -> list[SourceStanza]:
    """Parse every stanza of `package` from a `Sources` index.

    Stanzas without SHA-256 checksums are rejected rather than skipped: an index
    that cannot anchor its members to hashes cannot be the basis of a build.
    """
    stanzas: list[SourceStanza] = []
    for paragraph in _parse_paragraphs(text):
        if paragraph.get("Package") != package:
            continue
        for required in ("Version", "Directory", "Checksums-Sha256"):
            if required not in paragraph:
                raise MalformedIndex(
                    f"stanza for {package} is missing {required}: "
                    f"{paragraph.get('Version', '<no version>')}"
                )
        members = _parse_checksums(paragraph["Checksums-Sha256"])
        directory = paragraph["Directory"]
        segments = directory.split("/")
        if (
            not _DIRECTORY_RE.fullmatch(directory)
            or any(segment in ("", ".", "..") for segment in segments)
        ):
            raise MalformedIndex(f"unsafe Directory: {directory!r}")
        stanzas.append(
            SourceStanza(
                package=package,
                version=DebianVersion.parse(paragraph["Version"]),
                directory=directory,
                files=tuple(members[name] for name in sorted(members)),
            )
        )
    return stanzas


def select_newest(stanzas: list[SourceStanza]) -> SourceStanza:
    """Select the newest stanza by Debian version comparison.

    Never the first stanza, never a lexical maximum, and never a sort key
    derived from the file name.
    """
    if not stanzas:
        raise MalformedIndex("no stanzas to choose from")
    best = stanzas[0]
    for candidate in stanzas[1:]:
        if compare(candidate.version, best.version) > 0:
            best = candidate
    return best

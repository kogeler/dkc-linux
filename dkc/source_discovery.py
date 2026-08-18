"""Build a validated source inventory from authenticated Debian metadata."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import urlsplit

from .schema import validate
from .sources import SourceFile, parse_sources, select_newest

__all__ = ["build_inventory", "make_variables"]


_RELEASE_NAMES = {
    "Origin": "origin",
    "Label": "label",
    "Suite": "suite",
    "Codename": "codename",
    "Date": "date",
    "Valid-Until": "valid_until",
}


def _release(text: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for line in text.splitlines():
        prefix = "# release: "
        if not line.startswith(prefix):
            continue
        field, separator, value = line.removeprefix(prefix).partition(":")
        if not separator or not value.strip():
            raise ValueError("authenticated Release evidence is malformed")
        if field in _RELEASE_NAMES:
            result[_RELEASE_NAMES[field]] = value.strip()
        elif field == "Acquire-By-Hash":
            if value.strip() not in ("yes", "no"):
                raise ValueError("Acquire-By-Hash has an invalid value")
            result["acquire_by_hash"] = value.strip() == "yes"
    required = {"origin", "suite", "codename", "date"}
    if not required <= set(result):
        raise ValueError("authenticated Release evidence is incomplete")
    if result["origin"] != "Debian" or result["codename"] != "sid":
        raise ValueError("source metadata is not the Debian sid archive")
    return result


def _member(value: SourceFile, uri: str) -> dict[str, object]:
    return {
        "name": value.name,
        "sha256": value.sha256,
        "size": value.size,
        "uri": uri,
    }


def build_inventory(
    captured: str,
    *,
    mirror: str,
    discovered: datetime,
) -> dict[str, object]:
    if discovered.tzinfo is None:
        raise ValueError("discovery time must be timezone-aware")
    source_text = "\n".join(
        line for line in captured.splitlines() if not line.startswith("#")
    )
    selected = select_newest(parse_sources(source_text, "linux"))
    members = [
        _member(member, selected.uri(mirror, member)) for member in selected.files
    ]
    dsc_member = selected.dsc
    dsc = _member(dsc_member, selected.uri(mirror, dsc_member))
    result: dict[str, object] = {
        "schema": "dkc.source-inventory.v1",
        "source_package": "linux",
        "source_version": str(selected.version),
        "upstream_release": selected.version.upstream_release,
        "dsc": dsc,
        "members": members,
        "release": _release(captured),
        "maintainer_signature": "unavailable",
        "discovered_utc": discovered.astimezone(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
    }
    validate("source-inventory", result)
    return result


def make_variables(inventory: dict[str, object]) -> dict[str, str]:
    members = inventory["members"]
    if not isinstance(members, list):
        raise ValueError("source inventory members are malformed")
    by_suffix: dict[str, dict[str, object]] = {}
    for member in members:
        if not isinstance(member, dict):
            raise ValueError("source inventory member is malformed")
        name = member.get("name")
        if not isinstance(name, str):
            raise ValueError("source inventory member name is malformed")
        for suffix in (".dsc", ".orig.tar.xz", ".debian.tar.xz"):
            if name.endswith(suffix):
                if suffix in by_suffix:
                    raise ValueError(f"source inventory repeats {suffix}")
                by_suffix[suffix] = member
    if set(by_suffix) != {".dsc", ".orig.tar.xz", ".debian.tar.xz"} or len(members) != 3:
        raise ValueError("kernel source inventory is not the exact three-member graph")
    if inventory.get("dsc") != by_suffix[".dsc"]:
        raise ValueError("source inventory descriptor differs from its member graph")

    def fields(suffix: str, prefix: str) -> dict[str, str]:
        member = by_suffix[suffix]
        name = member.get("name")
        uri, digest, size = member.get("uri"), member.get("sha256"), member.get("size")
        if (
            not isinstance(name, str)
            or not isinstance(uri, str)
            or not isinstance(digest, str)
            or not isinstance(size, int)
        ):
            raise ValueError("source inventory member fields are malformed")
        if PurePosixPath(urlsplit(uri).path).name != name:
            raise ValueError("source inventory member name differs from its URI")
        return {
            f"DKC_{prefix}_NAME": name,
            f"DKC_{prefix}_URL": uri,
            f"DKC_{prefix}_SHA256": digest,
            f"DKC_{prefix}_SIZE": str(size),
        }

    version = inventory.get("source_version")
    if not isinstance(version, str):
        raise ValueError("source inventory version is malformed")
    result = {"DKC_SOURCE_VERSION": version}
    result.update(fields(".dsc", "DSC"))
    result.update(fields(".orig.tar.xz", "ORIG_TAR"))
    result.update(fields(".debian.tar.xz", "DEBIAN_TAR"))
    return result

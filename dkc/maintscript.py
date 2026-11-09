"""Bind metapackage migration scripts to their verified Debian source templates."""

from __future__ import annotations

import pathlib
import re
import shlex
import tarfile

from .debver import DebianVersion


def load_metapackage_templates(debian_archive: pathlib.Path) -> dict[str, str]:
    """Read one plain, bounded template per role without extracting the archive."""

    result: dict[str, str] = {}
    with tarfile.open(debian_archive) as archive:
        members = archive.getmembers()
        for role in ("image", "headers"):
            names = {
                f"debian/templates/{role}.meta.maintscript.{suffix}"
                for suffix in ("in", "j2")
            }
            matches = [member for member in members if member.name.removeprefix("./") in names]
            if len(matches) != 1:
                raise ValueError(f"{role} must have exactly one source maintscript template")
            member = matches[0]
            if not member.isfile() or not 0 < member.size <= 4096:
                raise ValueError(f"{role} maintscript template is not a bounded plain file")
            stream = archive.extractfile(member)
            if stream is None:
                raise ValueError(f"cannot read {role} maintscript template")
            result[role] = stream.read().decode("utf-8")
    return result


def metapackage_transition(template: str, package: str, kernel_release: str) -> list[str]:
    """Resolve data placeholders while rejecting executable template expressions."""

    match = re.fullmatch(r"dkc-linux-(image|headers)-(v[234]-amd64)", package)
    if (
        match is None
        or re.fullmatch(r"[A-Za-z0-9.+~-]+-v[234]-amd64", kernel_release) is None
        or not kernel_release.endswith("-" + match[2])
    ):
        raise ValueError("metapackage migration has a different kernel identity")
    role, suffix = match.groups()
    localversion = "-" + suffix
    variables = {
        "package": package,
        "abiname": kernel_release.removesuffix(localversion),
        "localversion": localversion,
    }

    def substitute(found: re.Match[str]) -> str:
        name = found[1] or found[2]
        if name not in variables:
            raise ValueError(f"unknown maintscript placeholder: {name}")
        return variables[name]

    rendered = re.sub(r"@([a-z_]+)@|\{\{\s*([a-z_]+)\s*\}\}", substitute, template)
    commands = [line.strip() for line in rendered.splitlines() if line.strip() and not line.startswith("#")]
    if len(commands) != 1:
        raise ValueError("metapackage source must declare one migration command")
    arguments = shlex.split(commands[0])
    if len(arguments) != 5 or arguments[0] != "dir_to_symlink":
        raise ValueError("unsupported metapackage source migration command")
    _, path, target, cutoff, owner = arguments
    if path not in {f"/usr/share/{directory}/{package}" for directory in ("doc", "bug")}:
        raise ValueError("metapackage migration leaves its own documentation or bug path")
    roles = ("image", "base") if role == "image" else ("headers",)
    if target not in {f"dkc-linux-{target_role}-{kernel_release}" for target_role in roles}:
        raise ValueError("metapackage migration targets a different kernel package")
    DebianVersion.parse(cutoff)
    if owner != package:
        raise ValueError("metapackage migration names a different owner")
    return ["dpkg-maintscript-helper", *arguments, "--", "$@"]

"""Parsing and filtering of Debian build-dependency fields.

Build dependencies carry two kinds of restriction that decide whether an entry
applies at all:

    libfoo-dev [amd64 arm64] <!nodoc !pkg.linux.notools>

Getting either wrong is expensive in opposite directions. Treat an inactive
entry as active and the build image grows dependencies it never needs; treat an
active entry as inactive and the build fails late, after the expensive part.

So the filtering is implemented once, here, with tests against the real kernel
control file, rather than being re-improvised in shell each time it is needed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = [
    "BUILD_DEPENDS_FIELDS",
    "Dependency",
    "all_build_depends",
    "control_fields",
    "filter_dependencies",
    "parse_field",
]

# All three fields, named once. A caller that assembles its own list will sooner
# or later forget one: `Build-Depends-Indep` is easy to overlook and it is where
# `rsync` is declared unconditionally, so omitting it produces a build image
# that passes a hand-rolled audit and then fails the real build.
BUILD_DEPENDS_FIELDS: tuple[str, ...] = (
    "Build-Depends",
    "Build-Depends-Arch",
    "Build-Depends-Indep",
)

# Split on commas that are not inside a bracketed architecture list.
_TOP_LEVEL_COMMA = re.compile(r",(?![^\[]*\])")
_ARCH_RESTRICTION = re.compile(r"\[([^\]]*)\]")
_PROFILE_GROUP = re.compile(r"<([^>]*)>")
_NAME = re.compile(r"^[^\s(\[<|]+")


@dataclass(frozen=True)
class Dependency:
    """One alternative-free build dependency with its restrictions."""

    name: str
    architectures: tuple[str, ...]
    profile_groups: tuple[tuple[str, ...], ...]
    raw: str

    def applies_to(self, architecture: str, profiles: frozenset[str]) -> bool:
        return self._architecture_matches(architecture) and self._profiles_match(profiles)

    def _architecture_matches(self, architecture: str) -> bool:
        if not self.architectures:
            return True
        negated = [a for a in self.architectures if a.startswith("!")]
        if negated:
            if len(negated) != len(self.architectures):
                # Debian forbids mixing; refuse rather than guess.
                raise ValueError(f"mixed negated architecture list in {self.raw!r}")
            return not any(a[1:] == architecture for a in negated)
        # `linux-any` and `any` are wildcards this project only needs to
        # evaluate for Linux architectures.
        return architecture in self.architectures or "linux-any" in self.architectures or (
            "any" in self.architectures
        )

    def _profiles_match(self, profiles: frozenset[str]) -> bool:
        if not self.profile_groups:
            return True
        # Groups are OR-ed; terms inside a group are AND-ed.
        return any(
            all(
                (term[1:] not in profiles) if term.startswith("!") else (term in profiles)
                for term in group
            )
            for group in self.profile_groups
        )


def parse_field(text: str) -> list[Dependency]:
    """Parse one `Build-Depends`-style field into dependencies.

    Alternatives (`a | b`) keep only the first alternative: Debian's own
    resolvers prefer it, and for an audit the first is the one that would be
    installed.
    """
    dependencies: list[Dependency] = []
    for item in _TOP_LEVEL_COMMA.split(text):
        item = " ".join(item.split())
        if not item:
            continue

        architectures: tuple[str, ...] = ()
        arch_match = _ARCH_RESTRICTION.search(item)
        if arch_match:
            architectures = tuple(arch_match.group(1).split())

        profile_groups = tuple(
            tuple(group.split()) for group in _PROFILE_GROUP.findall(item)
        )

        first_alternative = item.split("|")[0].strip()
        name_match = _NAME.match(first_alternative)
        if not name_match:
            raise ValueError(f"cannot parse a package name from {item!r}")
        name = name_match.group(0)
        # Multi-arch qualifiers are not part of the package name.
        for qualifier in (":native", ":any"):
            name = name.removesuffix(qualifier)

        dependencies.append(
            Dependency(
                name=name,
                architectures=architectures,
                profile_groups=profile_groups,
                raw=item,
            )
        )
    return dependencies


def filter_dependencies(
    dependencies: list[Dependency],
    architecture: str,
    profiles: frozenset[str],
) -> list[str]:
    """Names of the dependencies that actually apply, sorted and deduplicated."""
    return sorted(
        {d.name for d in dependencies if d.applies_to(architecture, profiles)}
    )


def all_build_depends(
    control: str,
    architecture: str,
    profiles: frozenset[str],
    *,
    include_indep: bool = True,
) -> list[str]:
    """Every build dependency that applies, across all three declaring fields.

    `include_indep` is False only for an architecture-only build. DKC publishes
    an architecture-independent headers-common package, so the default is True.
    """
    fields = BUILD_DEPENDS_FIELDS
    if not include_indep:
        fields = tuple(f for f in fields if f != "Build-Depends-Indep")

    dependencies: list[Dependency] = []
    for value in control_fields(control, fields).values():
        dependencies += parse_field(value)
    return filter_dependencies(dependencies, architecture, profiles)


def control_fields(control: str, names: tuple[str, ...]) -> dict[str, str]:
    """Extract named fields from the first paragraph of a control file."""
    paragraph = control.split("\n\n", 1)[0]
    fields: dict[str, str] = {}
    current: str | None = None
    for line in paragraph.splitlines():
        if line[:1] in (" ", "\t"):
            if current:
                fields[current] += " " + line.strip()
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        current = key if key in names else None
        if current:
            fields[current] = value.strip()
    return fields

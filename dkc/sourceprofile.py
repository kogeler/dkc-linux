"""Per-series Debian kernel source compatibility profiles.

Debian's `src:linux` packaging and the upstream kernel both change between
kernel series. Every DKC assumption that is only true for one series lives in
that series' profile, so supporting a newer series never rewrites the reviewed
assumptions that an older series still needs:

- `config/source-profiles/<id>/profile.toml` holds build inputs: the Debian
  build profiles and architecture inventory, the reviewed `CC_FLAGS_FPU`
  objects, and the reviewed final-artifact SIMD symbols;
- `config/source-profiles/<id>/validation.toml` holds acceptance inputs: the
  saved-Kbuild-command audit floors and exceptions and the exact-source
  kselftest profile;
- `debian-overlay/patches/<id>/` holds the GPL-2.0 packaging overlay;
- `tests/integration/kselftest-patches/<id>/` holds optional GPL-2.0 selftest
  source compatibility patches.

A profile covers one upstream `X.Y` series. Optional Debian version bounds
split a series only when Debian changes its packaging inside that series.
Selection is exact: a source version must match exactly one profile.
"""

from __future__ import annotations

import argparse
import pathlib
import re
import shlex
import sys
import tomllib
from dataclasses import dataclass
from typing import Any

from .debver import DebianVersion, InvalidVersion, compare

__all__ = [
    "BUILD_FILE",
    "KSELFTEST_PATCH_ROOT",
    "LTO_MODES",
    "OVERLAY_ROOT",
    "PROFILE_ROOT",
    "VALIDATION_FILE",
    "FpuObjectPolicy",
    "KbuildAuditPolicy",
    "KselftestPolicy",
    "SourceProfile",
    "SourceProfileError",
    "load_profile",
    "load_profiles",
    "select_profile",
]

PROFILE_ROOT = "config/source-profiles"
OVERLAY_ROOT = "debian-overlay/patches"
KSELFTEST_PATCH_ROOT = "tests/integration/kselftest-patches"
BUILD_FILE = "profile.toml"
VALIDATION_FILE = "validation.toml"
LTO_MODES: tuple[str, ...] = ("none", "thin", "full")

_ID_RE = re.compile(r"^(?P<series>[0-9]+\.[0-9]+)(?:-[a-z0-9]+)?$")
_PATCH_RE = re.compile(r"^[0-9]{4}-[a-z0-9][a-z0-9.-]*\.patch$")
_BUILD_PROFILE_RE = re.compile(r"^[a-z0-9][a-z0-9.+-]*$")
_ARCHITECTURE_RE = re.compile(r"^[a-z][a-z0-9]*$")
_OBJECT_RE = re.compile(r"^[A-Za-z0-9_+.-]+(?:/[A-Za-z0-9_+.-]+)*\.o$")
_ARTIFACT_RE = re.compile(r"^(?:vmlinux|kernel/[A-Za-z0-9_+.-]+(?:/[A-Za-z0-9_+.-]+)*\.ko)$")
_KSELFTEST_TARGET_RE = re.compile(r"^[A-Za-z0-9_.+-]+(?:/[A-Za-z0-9_.+-]+)*$")
_KSELFTEST_TEST_RE = re.compile(
    r"^[A-Za-z0-9_.+-]+(?:/[A-Za-z0-9_.+-]+)*:[A-Za-z0-9_.+/-]+$"
)
_KCONFIG_RE = re.compile(r"^[A-Z0-9_]+$")
_KIND_RE = re.compile(r"^[a-z][a-z0-9-]*$")


class SourceProfileError(ValueError):
    """A source profile is malformed, ambiguous, or absent."""


def _read_toml(path: pathlib.Path) -> dict[str, Any]:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise SourceProfileError(f"cannot read {path}: {exc}") from exc


def _fields(
    table: object,
    context: str,
    required: set[str],
    optional: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    if not isinstance(table, dict):
        raise SourceProfileError(f"{context} must be a table")
    missing = required - set(table)
    unexpected = set(table) - required - optional
    if missing or unexpected:
        raise SourceProfileError(
            f"{context} fields differ: missing={sorted(missing)}, "
            f"unexpected={sorted(unexpected)}"
        )
    return table


def _strings(
    value: object,
    context: str,
    pattern: re.Pattern[str],
    *,
    allow_empty: bool = False,
) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise SourceProfileError(f"{context} must be a string array")
    if not value and not allow_empty:
        raise SourceProfileError(f"{context} must not be empty")
    if len(value) != len(set(value)):
        raise SourceProfileError(f"{context} contains duplicates")
    unsafe = [item for item in value if not pattern.fullmatch(item)]
    if unsafe:
        raise SourceProfileError(f"{context} contains unsafe values: {unsafe[:5]}")
    return tuple(value)


def _integer(value: object, context: str, minimum: int, maximum: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
        raise SourceProfileError(f"{context} must be an integer in [{minimum}, {maximum}]")
    return value


def _version(value: object, context: str) -> str:
    if not isinstance(value, str):
        raise SourceProfileError(f"{context} must be a Debian version string")
    try:
        DebianVersion.parse(value)
    except InvalidVersion as exc:
        raise SourceProfileError(f"{context} is not a Debian version: {exc}") from exc
    return value


@dataclass(frozen=True)
class FpuObjectPolicy:
    """Objects that the kernel itself compiles with `CC_FLAGS_FPU`."""

    final_artifact: str
    objects: tuple[str, ...]


@dataclass(frozen=True)
class KbuildAuditPolicy:
    """Coverage floors and reviewed exceptions for saved Kbuild commands."""

    minimum_counts: dict[str, int]
    minimum_host_c: dict[str, int]
    lto_excluded_c_targets: frozenset[str]


@dataclass(frozen=True)
class KselftestPolicy:
    """The bounded exact-source kernel selftest profile."""

    kind: str
    targets: tuple[str, ...]
    tests: tuple[str, ...]
    v3_tests: tuple[str, ...]
    per_test_timeout: int
    aggregate_timeout: int
    required_builtin: tuple[str, ...]
    required_enabled: tuple[str, ...]

    def environment(self) -> str:
        """Render the shell assignments consumed by the selftest builder."""

        values = (
            ("DKC_KSELFTEST_PROFILE_KIND", self.kind),
            ("DKC_KSELFTEST_TARGETS", " ".join(self.targets)),
            ("DKC_KSELFTEST_TESTS", " ".join(self.tests)),
            ("DKC_KSELFTEST_V3_TESTS", " ".join(self.v3_tests)),
            ("DKC_KSELFTEST_PER_TEST_TIMEOUT", str(self.per_test_timeout)),
            ("DKC_KSELFTEST_AGGREGATE_TIMEOUT", str(self.aggregate_timeout)),
            ("DKC_KSELFTEST_REQUIRED_BUILTIN", " ".join(self.required_builtin)),
            ("DKC_KSELFTEST_REQUIRED_ENABLED", " ".join(self.required_enabled)),
        )
        return "".join(f"{name}={shlex.quote(value)}\n" for name, value in values)


@dataclass(frozen=True)
class SourceProfile:
    """Everything DKC assumes about one Debian kernel source series."""

    root: pathlib.Path
    profile_id: str
    kernel_series: tuple[int, int]
    reviewed_source_version: str
    first_source_version: str | None
    before_source_version: str | None
    build_profiles: tuple[str, ...]
    kernel_architectures: tuple[tuple[str, tuple[str, ...]], ...]
    fpu: FpuObjectPolicy
    simd_allowlist: tuple[dict[str, Any], ...]
    kbuild_audit: KbuildAuditPolicy
    kselftest: KselftestPolicy

    @property
    def directory(self) -> pathlib.Path:
        return self.root / PROFILE_ROOT / self.profile_id

    @property
    def build_file(self) -> pathlib.Path:
        return self.directory / BUILD_FILE

    @property
    def validation_file(self) -> pathlib.Path:
        return self.directory / VALIDATION_FILE

    @property
    def overlay_directory(self) -> pathlib.Path:
        return self.root / OVERLAY_ROOT / self.profile_id

    @property
    def overlay_patches(self) -> tuple[pathlib.Path, ...]:
        return tuple(sorted(self.overlay_directory.glob("*.patch")))

    @property
    def kselftest_patch_directory(self) -> pathlib.Path:
        return self.root / KSELFTEST_PATCH_ROOT / self.profile_id

    @property
    def kselftest_patches(self) -> tuple[pathlib.Path, ...]:
        return tuple(sorted(self.kselftest_patch_directory.glob("*.patch")))

    def covers(self, source_version: str) -> bool:
        version = DebianVersion.parse(source_version)
        if version.series != self.kernel_series:
            return False
        if self.first_source_version is not None and compare(
            version, self.first_source_version
        ) < 0:
            return False
        return self.before_source_version is None or compare(
            version, self.before_source_version
        ) < 0

    def build_policy_paths(self) -> tuple[pathlib.Path, ...]:
        """Profile files that are part of the kernel publication identity."""

        return (self.build_file, *self.overlay_patches)

    def validation_policy_paths(self) -> tuple[pathlib.Path, ...]:
        """Profile files that decide whether a built flavor is accepted."""

        return (self.validation_file, *self.kselftest_patches)

    def build_profiles_file(self) -> str:
        """Render the shell file embedded as `debian/dkc/build-profiles`."""

        return (
            f"# Debian build profiles from the DKC {self.profile_id} source profile.\n"
            f'DKC_BUILD_PROFILES="{" ".join(self.build_profiles)}"\n'
        )

    def architecture_restriction(self) -> str:
        """Render the Debian `config.local` that restricts the source to amd64."""

        lines = [
            "# The published source package intentionally supports only amd64.  Keep every",
            "# excluded Debian architecture explicit so an upstream inventory change fails",
            "# the preparation gate instead of silently expanding the package graph.",
        ]
        for kernel_architecture, debian_architectures in self.kernel_architectures:
            lines.extend(("", "[[kernelarch]]", f"name = '{kernel_architecture}'"))
            if "amd64" not in debian_architectures:
                lines.append("enable = false")
                continue
            for debian_architecture in debian_architectures:
                enabled = "true" if debian_architecture == "amd64" else "false"
                lines.extend(
                    (
                        "",
                        "  [[kernelarch.debianarch]]",
                        f"  name = '{debian_architecture}'",
                        f"  enable = {enabled}",
                    )
                )
        return "\n".join(lines) + "\n"


def _load_build(path: pathlib.Path, profile_id: str) -> dict[str, Any]:
    raw = _fields(
        _read_toml(path),
        str(path),
        {
            "schema_version",
            "kernel_series",
            "reviewed_source_version",
            "debian",
            "fpu",
            "simd_allowlist",
        },
        frozenset({"first_source_version", "before_source_version"}),
    )
    if raw["schema_version"] != 1:
        raise SourceProfileError(f"{path} has an unsupported schema")
    match = _ID_RE.fullmatch(profile_id)
    if match is None or raw["kernel_series"] != match.group("series"):
        raise SourceProfileError(f"{path} kernel_series does not match profile {profile_id}")
    return raw


def _load_validation(path: pathlib.Path, series: str) -> dict[str, Any]:
    raw = _fields(
        _read_toml(path),
        str(path),
        {"schema_version", "kernel_series", "kbuild_audit", "kselftest"},
    )
    if raw["schema_version"] != 1 or raw["kernel_series"] != series:
        raise SourceProfileError(f"{path} does not describe kernel series {series}")
    return raw


def _architectures(value: object, context: str) -> tuple[tuple[str, tuple[str, ...]], ...]:
    if not isinstance(value, dict) or not value:
        raise SourceProfileError(f"{context} must be a non-empty table")
    result: list[tuple[str, tuple[str, ...]]] = []
    for name, debian in value.items():
        if not _ARCHITECTURE_RE.fullmatch(name):
            raise SourceProfileError(f"{context} has an unsafe kernel architecture {name!r}")
        result.append((name, _strings(debian, f"{context}.{name}", _ARCHITECTURE_RE)))
    amd64 = [name for name, debian in result if "amd64" in debian]
    if amd64 != ["x86"]:
        raise SourceProfileError(f"{context} must place amd64 only under x86")
    return tuple(result)


def load_profile(directory: pathlib.Path) -> SourceProfile:
    """Load and validate one profile directory."""

    profile_id = directory.name
    match = _ID_RE.fullmatch(profile_id)
    if match is None:
        raise SourceProfileError(f"invalid source profile directory name {profile_id!r}")
    if len(directory.parents) < 3 or directory.parent.as_posix().split("/")[-2:] != [
        *PROFILE_ROOT.split("/")
    ]:
        raise SourceProfileError(f"{directory} is not below {PROFILE_ROOT}")
    root = directory.parents[2]
    if not directory.is_dir() or directory.is_symlink():
        raise SourceProfileError(f"{directory} is not a profile directory")
    names = {path.name for path in directory.iterdir()}
    if names != {BUILD_FILE, VALIDATION_FILE}:
        raise SourceProfileError(
            f"profile {profile_id} must contain exactly {BUILD_FILE} and {VALIDATION_FILE}"
        )

    build = _load_build(directory / BUILD_FILE, profile_id)
    validation = _load_validation(directory / VALIDATION_FILE, build["kernel_series"])
    major, minor = (int(part) for part in match.group("series").split("."))

    debian = _fields(build["debian"], "debian", {"build_profiles", "kernel_architectures"})
    fpu_raw = _fields(build["fpu"], "fpu", {"final_artifact", "objects"})
    final_artifact = fpu_raw["final_artifact"]
    if not isinstance(final_artifact, str) or not _ARTIFACT_RE.fullmatch(final_artifact):
        raise SourceProfileError("fpu.final_artifact must be one exact module path")
    objects = _strings(fpu_raw["objects"], "fpu.objects", _OBJECT_RE)
    if list(objects) != sorted(objects) or any(".." in item.split("/") for item in objects):
        raise SourceProfileError("fpu.objects must be sorted safe relative objects")
    simd_allowlist = build["simd_allowlist"]
    if (
        not isinstance(simd_allowlist, list)
        or not simd_allowlist
        or not all(isinstance(entry, dict) for entry in simd_allowlist)
    ):
        raise SourceProfileError("simd_allowlist must be a non-empty array of tables")

    kbuild = _fields(
        validation["kbuild_audit"],
        "kbuild_audit",
        {
            "minimum_records",
            "minimum_normal_c",
            "minimum_special_c",
            "minimum_kernel_rust",
            "minimum_host_c",
            "lto_excluded_c_targets",
        },
    )
    host_c = _fields(kbuild["minimum_host_c"], "kbuild_audit.minimum_host_c", set(LTO_MODES))
    exclusions = _strings(
        kbuild["lto_excluded_c_targets"], "kbuild_audit.lto_excluded_c_targets", _OBJECT_RE
    )
    if list(exclusions) != sorted(exclusions):
        raise SourceProfileError("kbuild_audit.lto_excluded_c_targets must be sorted")

    selftest = _fields(
        validation["kselftest"],
        "kselftest",
        {
            "kind",
            "targets",
            "tests",
            "v3_tests",
            "per_test_timeout",
            "aggregate_timeout",
            "required_builtin",
            "required_enabled",
        },
    )
    kind = selftest["kind"]
    if not isinstance(kind, str) or not _KIND_RE.fullmatch(kind):
        raise SourceProfileError("kselftest.kind is unsafe")
    targets = _strings(selftest["targets"], "kselftest.targets", _KSELFTEST_TARGET_RE)
    tests = _strings(selftest["tests"], "kselftest.tests", _KSELFTEST_TEST_RE)
    v3_tests = _strings(
        selftest["v3_tests"], "kselftest.v3_tests", _KSELFTEST_TEST_RE, allow_empty=True
    )
    if {test.split(":", 1)[0] for test in tests} != set(targets):
        raise SourceProfileError("every kselftest target must run at least one selected test")
    if not set(v3_tests) <= set(tests):
        raise SourceProfileError("kselftest.v3_tests must be selected tests")
    required_builtin = _strings(
        selftest["required_builtin"], "kselftest.required_builtin", _KCONFIG_RE
    )
    required_enabled = _strings(
        selftest["required_enabled"],
        "kselftest.required_enabled",
        _KCONFIG_RE,
        allow_empty=True,
    )
    if not set(required_builtin).isdisjoint(required_enabled):
        raise SourceProfileError("kselftest builtin and enabled requirements overlap")

    first = build.get("first_source_version")
    before = build.get("before_source_version")
    profile = SourceProfile(
        root=root,
        profile_id=profile_id,
        kernel_series=(major, minor),
        reviewed_source_version=_version(
            build["reviewed_source_version"], "reviewed_source_version"
        ),
        first_source_version=None if first is None else _version(first, "first_source_version"),
        before_source_version=None if before is None else _version(before, "before_source_version"),
        build_profiles=_strings(
            debian["build_profiles"], "debian.build_profiles", _BUILD_PROFILE_RE
        ),
        kernel_architectures=_architectures(
            debian["kernel_architectures"], "debian.kernel_architectures"
        ),
        fpu=FpuObjectPolicy(final_artifact=final_artifact, objects=objects),
        simd_allowlist=tuple(simd_allowlist),
        kbuild_audit=KbuildAuditPolicy(
            minimum_counts={
                "records": _integer(kbuild["minimum_records"], "minimum_records", 1, 10**7),
                "normal_c": _integer(kbuild["minimum_normal_c"], "minimum_normal_c", 1, 10**7),
                "special_c": _integer(kbuild["minimum_special_c"], "minimum_special_c", 1, 10**7),
                "kernel_rust": _integer(
                    kbuild["minimum_kernel_rust"], "minimum_kernel_rust", 1, 10**7
                ),
            },
            minimum_host_c={
                mode: _integer(host_c[mode], f"minimum_host_c.{mode}", 1, 10**7)
                for mode in LTO_MODES
            },
            lto_excluded_c_targets=frozenset(exclusions),
        ),
        kselftest=KselftestPolicy(
            kind=kind,
            targets=targets,
            tests=tests,
            v3_tests=v3_tests,
            per_test_timeout=_integer(selftest["per_test_timeout"], "per_test_timeout", 1, 600),
            aggregate_timeout=_integer(
                selftest["aggregate_timeout"], "aggregate_timeout", 1, 3600
            ),
            required_builtin=required_builtin,
            required_enabled=required_enabled,
        ),
    )
    if (
        profile.first_source_version is not None
        and profile.before_source_version is not None
        and compare(profile.first_source_version, profile.before_source_version) >= 0
    ):
        raise SourceProfileError(f"profile {profile_id} has an empty source version range")
    if not profile.covers(profile.reviewed_source_version):
        raise SourceProfileError(
            f"profile {profile_id} does not cover its reviewed source version"
        )
    return profile


def _require_series_directories(
    base: pathlib.Path, profile_ids: set[str], *, required: bool
) -> None:
    if not base.is_dir():
        if required:
            raise SourceProfileError(f"{base} is absent")
        return
    for entry in sorted(base.iterdir()):
        if entry.name == "README.md" and entry.is_file():
            continue
        if not entry.is_dir() or entry.is_symlink() or entry.name not in profile_ids:
            raise SourceProfileError(f"{entry} does not belong to a source profile")
        patches = sorted(entry.iterdir())
        if any(
            not path.is_file() or path.is_symlink() or not _PATCH_RE.fullmatch(path.name)
            for path in patches
        ):
            raise SourceProfileError(f"{entry} contains a non-patch entry")
    if required:
        missing = sorted(
            profile_id
            for profile_id in profile_ids
            if not any((base / profile_id).glob("*.patch"))
        )
        if missing:
            raise SourceProfileError(f"source profiles lack overlay patches: {missing}")


def load_profiles(root: pathlib.Path) -> tuple[SourceProfile, ...]:
    """Load the complete registry and reject orphaned or overlapping profiles."""

    base = root / PROFILE_ROOT
    if not base.is_dir():
        raise SourceProfileError(f"{PROFILE_ROOT} is absent")
    profiles: list[SourceProfile] = []
    for entry in sorted(base.iterdir()):
        if entry.name == "README.md" and entry.is_file():
            continue
        if not entry.is_dir() or entry.is_symlink():
            raise SourceProfileError(f"unexpected entry in {PROFILE_ROOT}: {entry.name}")
        profiles.append(load_profile(entry))
    if not profiles:
        raise SourceProfileError("no source profiles are defined")
    profile_ids = {profile.profile_id for profile in profiles}
    _require_series_directories(root / OVERLAY_ROOT, profile_ids, required=True)
    _require_series_directories(root / KSELFTEST_PATCH_ROOT, profile_ids, required=False)

    by_series: dict[tuple[int, int], list[SourceProfile]] = {}
    for profile in profiles:
        by_series.setdefault(profile.kernel_series, []).append(profile)
    for series, members in by_series.items():
        if len(members) == 1:
            continue
        members.sort(
            key=lambda item: DebianVersion.parse(item.first_source_version or "0~")
        )
        for earlier, later in zip(members, members[1:]):
            if (
                earlier.before_source_version is None
                or later.first_source_version is None
                or compare(earlier.before_source_version, later.first_source_version) > 0
            ):
                raise SourceProfileError(
                    f"source profiles for {series[0]}.{series[1]} overlap: "
                    f"{earlier.profile_id}, {later.profile_id}"
                )
    return tuple(profiles)


def select_profile(root: pathlib.Path, source_version: str) -> SourceProfile:
    """Return the single profile that covers one Debian source version."""

    try:
        DebianVersion.parse(source_version)
    except InvalidVersion as exc:
        raise SourceProfileError(f"invalid Debian source version: {exc}") from exc
    matches = [profile for profile in load_profiles(root) if profile.covers(source_version)]
    if not matches:
        raise SourceProfileError(
            f"no source profile covers linux {source_version}; review the new "
            f"Debian source and add a profile under {PROFILE_ROOT}"
        )
    if len(matches) != 1:
        raise SourceProfileError(f"linux {source_version} matches several source profiles")
    return matches[0]


def main(argv: list[str] | None = None) -> int:
    queries = (
        "id",
        "directory",
        "overlay-directory",
        "kselftest-patch-directory",
        "build-profiles",
        "kselftest-environment",
    )
    parser = argparse.ArgumentParser(
        prog="python3 -m dkc.sourceprofile",
        description="Resolve the source profile that covers one Debian linux version.",
    )
    parser.add_argument("root", type=pathlib.Path)
    parser.add_argument("source_version")
    parser.add_argument("query", choices=queries)
    args = parser.parse_args(argv)
    try:
        profile = select_profile(args.root, args.source_version)
    except SourceProfileError as exc:
        print(f"source profile: {exc}", file=sys.stderr)
        return 1
    output = {
        "id": profile.profile_id + "\n",
        "directory": f"{profile.directory}\n",
        "overlay-directory": f"{profile.overlay_directory}\n",
        "kselftest-patch-directory": f"{profile.kselftest_patch_directory}\n",
        "build-profiles": " ".join(profile.build_profiles) + "\n",
        "kselftest-environment": profile.kselftest.environment(),
    }[args.query]
    sys.stdout.write(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

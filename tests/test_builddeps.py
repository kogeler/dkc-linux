"""Dependency filtering, tested against the real kernel control file."""

from __future__ import annotations

import pathlib
import re

import pytest

from dkc.builddeps import (
    BUILD_DEPENDS_FIELDS,
    all_build_depends,
    control_fields,
    filter_dependencies,
    parse_field,
)

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "control-linux-7.1.7-1.txt"
PROFILES_FILE = pathlib.Path(__file__).parent.parent / "config" / "build-profiles"


@pytest.fixture(scope="module")
def control() -> str:
    if not FIXTURE.exists():
        pytest.skip(f"fixture missing: {FIXTURE}")
    return "\n".join(
        line for line in FIXTURE.read_text().splitlines() if not line.startswith("#")
    ).lstrip("\n")


@pytest.fixture(scope="module")
def declared(control: str) -> list:
    fields = control_fields(control, ("Build-Depends", "Build-Depends-Arch"))
    deps = []
    for value in fields.values():
        deps += parse_field(value)
    return deps


@pytest.fixture(scope="module")
def configured_profiles() -> frozenset[str]:
    match = re.search(r'DKC_BUILD_PROFILES="([^"]*)"', PROFILES_FILE.read_text())
    assert match, "config/build-profiles must define DKC_BUILD_PROFILES"
    return frozenset(match.group(1).split())


# --------------------------------------------------------------------------
# Restriction syntax
# --------------------------------------------------------------------------


def test_architecture_restriction() -> None:
    deps = parse_field("libfoo-dev [amd64 arm64], libbar-dev [s390x]")
    assert filter_dependencies(deps, "amd64", frozenset()) == ["libfoo-dev"]
    assert filter_dependencies(deps, "s390x", frozenset()) == ["libbar-dev"]


def test_negated_architecture() -> None:
    deps = parse_field("libfoo-dev [!amd64]")
    assert filter_dependencies(deps, "amd64", frozenset()) == []
    assert filter_dependencies(deps, "arm64", frozenset()) == ["libfoo-dev"]


def test_mixed_negation_is_refused() -> None:
    """Debian forbids mixing; guessing an interpretation would be worse."""
    deps = parse_field("libfoo-dev [amd64 !arm64]")
    with pytest.raises(ValueError, match="mixed negated"):
        filter_dependencies(deps, "amd64", frozenset())


def test_linux_any_matches() -> None:
    deps = parse_field("libelf-dev [linux-any]")
    assert filter_dependencies(deps, "amd64", frozenset()) == ["libelf-dev"]


def test_negative_profile_term() -> None:
    """`<!nodoc>` means: needed unless the nodoc profile is active."""
    deps = parse_field("asciidoctor <!nodoc>")
    assert filter_dependencies(deps, "amd64", frozenset()) == ["asciidoctor"]
    assert filter_dependencies(deps, "amd64", frozenset({"nodoc"})) == []


def test_positive_profile_term() -> None:
    deps = parse_field("libfoo-dev <stage1>")
    assert filter_dependencies(deps, "amd64", frozenset()) == []
    assert filter_dependencies(deps, "amd64", frozenset({"stage1"})) == ["libfoo-dev"]


def test_terms_inside_a_group_are_anded() -> None:
    deps = parse_field("libfoo-dev <!nodoc !pkg.linux.notools>")
    assert filter_dependencies(deps, "amd64", frozenset()) == ["libfoo-dev"]
    assert filter_dependencies(deps, "amd64", frozenset({"nodoc"})) == []
    assert filter_dependencies(deps, "amd64", frozenset({"pkg.linux.notools"})) == []


def test_groups_are_ored() -> None:
    deps = parse_field("libfoo-dev <stage1> <cross>")
    assert filter_dependencies(deps, "amd64", frozenset({"cross"})) == ["libfoo-dev"]
    assert filter_dependencies(deps, "amd64", frozenset({"stage1"})) == ["libfoo-dev"]
    assert filter_dependencies(deps, "amd64", frozenset()) == []


def test_multiarch_qualifiers_are_stripped() -> None:
    deps = parse_field("python3:native, libelf-dev:any")
    assert filter_dependencies(deps, "amd64", frozenset()) == ["libelf-dev", "python3"]


def test_version_constraints_are_not_part_of_the_name() -> None:
    deps = parse_field("rustc:native (>= 1.78.0) [amd64]")
    assert filter_dependencies(deps, "amd64", frozenset()) == ["rustc"]


def test_alternatives_take_the_first() -> None:
    deps = parse_field("foo | bar")
    assert filter_dependencies(deps, "amd64", frozenset()) == ["foo"]


def test_commas_inside_architecture_lists_do_not_split() -> None:
    deps = parse_field("libfoo-dev [amd64 arm64], libbar-dev")
    assert len(deps) == 2


# --------------------------------------------------------------------------
# Against the real control file
# --------------------------------------------------------------------------


def test_rust_is_required_on_amd64(declared: list) -> None:
    """Debian enables Rust on amd64, so these must survive filtering."""
    names = filter_dependencies(declared, "amd64", frozenset())
    for required in ("rustc", "rust-src", "bindgen"):
        assert required in names, required


def test_rust_stays_required_under_the_configured_profiles(
    declared: list, configured_profiles: frozenset[str]
) -> None:
    """Disabling Rust to simplify the build is forbidden, so norust is not set."""
    names = filter_dependencies(declared, "amd64", configured_profiles)
    assert "rustc" in names
    assert "rust-src" in names
    assert "bindgen" in names


def test_debug_package_is_excluded_without_disabling_btf(
    configured_profiles: frozenset[str],
) -> None:
    assert "pkg.linux.nokerneldbg" in configured_profiles
    assert "pkg.linux.nokerneldbginfo" not in configured_profiles


def test_installer_dependencies_and_packages_have_both_profiles(
    configured_profiles: frozenset[str],
) -> None:
    assert {"noudeb", "pkg.linux.noudeb"} <= configured_profiles


def test_kernel_essentials_survive_the_profiles(
    declared: list, configured_profiles: frozenset[str]
) -> None:
    names = filter_dependencies(declared, "amd64", configured_profiles)
    for required in ("bc", "bison", "flex", "kmod", "libelf-dev", "libssl-dev", "pahole"):
        assert required in names, required


def test_profiles_drop_the_tools_and_docs_closure(
    declared: list, configured_profiles: frozenset[str]
) -> None:
    """The measured reason the profile set exists."""
    without = filter_dependencies(declared, "amd64", frozenset())
    with_profiles = filter_dependencies(declared, "amd64", configured_profiles)

    assert len(with_profiles) < len(without) / 1.5, (
        f"profiles should roughly halve the closure: {len(without)} -> {len(with_profiles)}"
    )
    for dropped in (
        "asciidoctor",
        "libaudit-dev",
        "libdw-dev",
        "libnuma-dev",
        "libunwind-dev",
        "python3-docutils",
        "gcc-multilib",
    ):
        assert dropped in without, f"{dropped} should be declared without profiles"
        assert dropped not in with_profiles, f"{dropped} should be dropped by profiles"


def test_the_trixie_gap_is_visible_in_the_filtered_set(
    declared: list, configured_profiles: frozenset[str]
) -> None:
    """gcc-15-for-host survives every profile, so dependency auditing is required."""
    names = filter_dependencies(declared, "amd64", configured_profiles)
    assert "gcc-15-for-host" in names


def test_other_architectures_do_not_leak_in(declared: list) -> None:
    names = filter_dependencies(declared, "amd64", frozenset())
    assert "gcc-15-hppa64-linux-gnu" not in names
    assert "gcc-arm-linux-gnueabihf" not in names


# --------------------------------------------------------------------------
# All three declaring fields
# --------------------------------------------------------------------------


def test_all_three_fields_are_declared_in_one_place() -> None:
    assert BUILD_DEPENDS_FIELDS == (
        "Build-Depends",
        "Build-Depends-Arch",
        "Build-Depends-Indep",
    )


def test_control_file_actually_uses_all_three(control: str) -> None:
    """If the source stops using one, the audit should be revisited, not silently pass."""
    for field in BUILD_DEPENDS_FIELDS:
        assert f"{field}:" in control, field


def test_rsync_comes_from_build_depends_indep(
    control: str, configured_profiles: frozenset[str]
) -> None:
    """The regression this helper exists to prevent.

    rsync is declared unconditionally in Build-Depends-Indep. An audit that
    parses only Build-Depends and Build-Depends-Arch declares the build image
    complete while rsync is missing, and dpkg-checkbuilddeps then fails.
    """
    complete = all_build_depends(control, "amd64", configured_profiles)
    arch_only = all_build_depends(
        control, "amd64", configured_profiles, include_indep=False
    )
    assert "rsync" in complete
    assert "rsync" not in arch_only


def test_indep_documentation_stack_is_dropped_by_nodoc(
    control: str, configured_profiles: frozenset[str]
) -> None:
    names = all_build_depends(control, "amd64", configured_profiles)
    for dropped in ("dvipng", "graphviz", "python3-sphinx", "texlive-latex-base"):
        assert dropped not in names, dropped


def test_cpio_survives_because_its_second_profile_group_matches(
    control: str, configured_profiles: frozenset[str]
) -> None:
    """cpio is declared as <!nodoc !pkg.linux.quick> <!pkg.linux.nokernel>.

    The first group is false under nodoc, the second is true, and the groups are
    OR-ed, so cpio is required. Getting the OR wrong drops a package the build
    needs.
    """
    assert "cpio" in all_build_depends(control, "amd64", configured_profiles)

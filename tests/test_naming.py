"""DKC identity: versions, ABI, kernel release, package names, revisions."""

from __future__ import annotations

import shutil
import subprocess

import pytest

from dkc.debver import compare
from dkc.naming import (
    FLAVORS,
    UTS_RELEASE_MAX,
    Identity,
    InvalidIdentity,
    package_names,
)

BUILD_ID = "a1b2c3d4e5f6"


@pytest.fixture
def identity() -> Identity:
    return Identity.create("7.1.7-1", 1, BUILD_ID)


def test_package_version_extends_the_debian_version(identity: Identity) -> None:
    assert identity.package_version == "7.1.7-1+dkc13.1"


def test_package_version_sorts_above_its_debian_source(identity: Identity) -> None:
    assert compare(identity.package_version, "7.1.7-1") > 0
    # and below the next Debian revision, so a Debian update always wins
    assert compare(identity.package_version, "7.1.7-2") < 0


def test_dkc_revisions_order(identity: Identity) -> None:
    second = Identity.create("7.1.7-1", 2, BUILD_ID)
    assert compare(second.package_version, identity.package_version) > 0
    tenth = Identity.create("7.1.7-1", 10, BUILD_ID)
    assert compare(tenth.package_version, second.package_version) > 0


def test_epoch_is_preserved_in_the_package_version() -> None:
    ident = Identity.create("1:7.1.7-1", 1, BUILD_ID)
    assert ident.package_version == "1:7.1.7-1+dkc13.1"


def test_epoch_never_reaches_the_kernel_release() -> None:
    """An epoch in a path or in uname -r would be a defect, not a cosmetic issue."""
    ident = Identity.create("1:7.1.7-1", 1, BUILD_ID)
    for flavor in FLAVORS:
        assert ":" not in ident.kernel_release(flavor)


def test_abi_is_shared_by_all_flavors(identity: Identity) -> None:
    abis = {identity.kernel_release(f).removesuffix(f"-{f}-amd64") for f in FLAVORS}
    assert abis == {identity.abi}


def test_kernel_release_is_unique_per_flavor(identity: Identity) -> None:
    releases = {identity.kernel_release(f) for f in FLAVORS}
    assert len(releases) == len(FLAVORS)


def test_kernel_release_shape(identity: Identity) -> None:
    assert identity.kernel_release("v3") == f"7.1.7+dkc13.r1.g{BUILD_ID}-v3-amd64"


def test_kernel_release_fits_the_uts_limit(identity: Identity) -> None:
    for flavor in FLAVORS:
        assert len(identity.kernel_release(flavor).encode()) <= UTS_RELEASE_MAX


def test_kernel_release_rejects_an_over_long_identity() -> None:
    """A long upstream release plus a long build id must fail loudly, not truncate."""
    ident = Identity.create("7.1.7", 1, "a" * 40)
    ident_long = Identity(
        debian_source_version=ident.debian_source_version,
        dkc_revision=99999999,
        build_id="b" * 48,
    )
    with pytest.raises(InvalidIdentity, match="UTS_RELEASE"):
        ident_long.kernel_release("v4")


def test_unknown_flavor_is_rejected(identity: Identity) -> None:
    with pytest.raises(InvalidIdentity):
        identity.kernel_release("v5")


@pytest.mark.parametrize("bad_id", ["short", "A1B2C3D4E5F6", "a1b2c3d4e5g6", ""])
def test_build_id_must_be_lowercase_hex_and_long_enough(bad_id: str) -> None:
    with pytest.raises(InvalidIdentity):
        Identity.create("7.1.7-1", 1, bad_id)


@pytest.mark.parametrize("bad_rev", [0, -1])
def test_revision_must_be_positive(bad_rev: int) -> None:
    with pytest.raises(InvalidIdentity):
        Identity.create("7.1.7-1", bad_rev, BUILD_ID)


# --------------------------------------------------------------------------
# Package graph
# --------------------------------------------------------------------------


def test_package_names_cover_every_flavor_role(identity: Identity) -> None:
    names = package_names(identity)
    for flavor in FLAVORS:
        for role in ("base", "binary", "modules", "image", "headers"):
            expected = f"dkc-linux-{role}-{identity.abi}-{flavor}-amd64"
            assert expected in names["versioned"], expected


def test_package_names_include_common_and_kbuild(identity: Identity) -> None:
    names = package_names(identity)
    assert f"dkc-linux-headers-{identity.abi}-common" in names["versioned"]
    assert f"dkc-linux-kbuild-{identity.abi}" in names["versioned"]


def test_meta_packages_are_version_independent(identity: Identity) -> None:
    """Meta-packages are the stable upgrade path, so they must not carry the ABI."""
    names = package_names(identity)
    assert names["meta"] == [
        "dkc-linux-base-v2-amd64",
        "dkc-linux-base-v3-amd64",
        "dkc-linux-base-v4-amd64",
        "dkc-linux-image-v2-amd64",
        "dkc-linux-image-v3-amd64",
        "dkc-linux-image-v4-amd64",
        "dkc-linux-headers-v2-amd64",
        "dkc-linux-headers-v3-amd64",
        "dkc-linux-headers-v4-amd64",
    ]
    for name in names["meta"]:
        assert identity.abi not in name


def test_every_package_name_is_namespaced(identity: Identity) -> None:
    names = package_names(identity)
    for group in names.values():
        for name in group:
            assert name.startswith("dkc-"), name


def test_package_names_do_not_collide_with_debian(identity: Identity) -> None:
    """A DKC name must never equal the official Debian name it mirrors."""
    names = package_names(identity)
    for group in names.values():
        for name in group:
            assert not name.startswith("linux-")


def test_two_dkc_versions_of_one_flavor_coexist() -> None:
    """Versioned packages must differ between revisions so both can be installed."""
    first = Identity.create("7.1.7-1", 1, "a" * 12)
    second = Identity.create("7.1.7-1", 2, "b" * 12)
    assert set(package_names(first)["versioned"]).isdisjoint(
        package_names(second)["versioned"]
    )


@pytest.mark.skipif(shutil.which("dpkg") is None, reason="dpkg is not installed")
def test_generated_versions_are_valid_for_dpkg() -> None:
    for revision in (1, 2, 10, 1234):
        version = Identity.create("7.1.7-1", revision, BUILD_ID).package_version
        assert (
            subprocess.run(
                ["dpkg", "--validate-version", version], check=False
            ).returncode
            == 0
        ), version

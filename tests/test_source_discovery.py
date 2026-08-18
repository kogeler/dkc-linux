from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from dkc.source_discovery import build_inventory, make_variables


FIXTURE = Path(__file__).parent / "fixtures/sources-linux-sid.txt"


def inventory():
    return build_inventory(
        FIXTURE.read_text(),
        mirror="http://deb.debian.org/debian",
        discovered=datetime(2026, 8, 10, 18, 9, 12, tzinfo=timezone.utc),
    )


def test_source_discovery_selects_and_exports_one_exact_build_graph() -> None:
    value = inventory()
    variables = make_variables(value)
    assert value["source_version"] == "7.1.7-1"
    assert variables["DKC_SOURCE_VERSION"] == "7.1.7-1"
    assert variables["DKC_DSC_NAME"] == "linux_7.1.7-1.dsc"
    assert variables["DKC_ORIG_TAR_NAME"] == "linux_7.1.7.orig.tar.xz"
    assert variables["DKC_DEBIAN_TAR_NAME"] == "linux_7.1.7-1.debian.tar.xz"
    assert variables["DKC_DSC_URL"].endswith("linux_7.1.7-1.dsc")
    assert variables["DKC_ORIG_TAR_URL"].endswith("linux_7.1.7.orig.tar.xz")
    assert variables["DKC_DEBIAN_TAR_URL"].endswith("linux_7.1.7-1.debian.tar.xz")


def test_source_discovery_exports_updated_member_names_without_a_version_template() -> None:
    captured = (
        FIXTURE.read_text()
        .replace("Version: 7.1.7-1", "Version: 7.1.8-2")
        .replace("linux_7.1.7-1", "linux_7.1.8-2")
        .replace("linux_7.1.7.orig", "linux_7.1.8.orig")
    )
    value = build_inventory(
        captured,
        mirror="http://deb.debian.org/debian",
        discovered=datetime(2026, 8, 17, 19, 0, tzinfo=timezone.utc),
    )
    variables = make_variables(value)
    assert variables["DKC_SOURCE_VERSION"] == "7.1.8-2"
    assert variables["DKC_DSC_NAME"] == "linux_7.1.8-2.dsc"
    assert variables["DKC_ORIG_TAR_NAME"] == "linux_7.1.8.orig.tar.xz"
    assert variables["DKC_DEBIAN_TAR_NAME"] == "linux_7.1.8-2.debian.tar.xz"


def test_source_discovery_rejects_a_member_name_that_differs_from_its_uri() -> None:
    value = inventory()
    members = value["members"]
    assert isinstance(members, list)
    orig = next(
        member
        for member in members
        if isinstance(member, dict)
        and isinstance(member.get("name"), str)
        and member["name"].endswith(".orig.tar.xz")
    )
    orig["name"] = "linux_wrong.orig.tar.xz"
    with pytest.raises(ValueError, match="differs from its URI"):
        make_variables(value)


def test_source_discovery_rejects_a_descriptor_member_identity_split() -> None:
    value = inventory()
    descriptor = value["dsc"]
    assert isinstance(descriptor, dict)
    descriptor["sha256"] = "0" * 64
    with pytest.raises(ValueError, match="descriptor differs"):
        make_variables(value)


def test_source_discovery_binds_the_authenticated_release() -> None:
    release = inventory()["release"]
    assert isinstance(release, dict)
    assert release["origin"] == "Debian"
    assert release["codename"] == "sid"
    assert release["acquire_by_hash"] is True


def test_source_discovery_rejects_an_unexpected_archive_identity() -> None:
    captured = FIXTURE.read_text().replace(
        "# release: Origin: Debian", "# release: Origin: Example"
    )
    with pytest.raises(ValueError, match="not the Debian sid"):
        build_inventory(
            captured,
            mirror="http://deb.debian.org/debian",
            discovered=datetime.now(timezone.utc),
        )

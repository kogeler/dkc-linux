"""DKC identity: versions, ABI, kernel release, and binary package names.

Identity is security-relevant. Two publications that differ in bytes must never
share an identity, and one publication's three flavors must share everything
except the flavor suffix. Every value here is derived, validated, and never
assembled ad hoc at a call site.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .debver import DebianVersion

__all__ = [
    "FLAVORS",
    "Identity",
    "InvalidIdentity",
    "UTS_RELEASE_MAX",
    "package_names",
]

FLAVORS: tuple[str, ...] = ("v2", "v3", "v4")

# include/linux/utsname.h stores the release in char[__NEW_UTS_LEN + 1] with
# __NEW_UTS_LEN == 64, so the string itself may be at most 64 bytes.
UTS_RELEASE_MAX = 64

# The DKC suffix marks the userspace this kernel is built for: Debian 13.
_DKC_SUFFIX = "dkc13"

# A kernel release becomes a directory name under /lib/modules and a file name
# under /boot, so it is restricted to a conservative safe set.
_KREL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+~-]*$")
_BUILD_ID_RE = re.compile(r"^[0-9a-f]{12,}$")


class InvalidIdentity(ValueError):
    """A derived identity value failed validation."""


@dataclass(frozen=True)
class Identity:
    """Everything derived from one Debian source version and one DKC revision.

    `build_id` is the displayed prefix of the publication-wide build-input
    digest. All three flavors of one publication share it; the flavor suffix is
    what makes each kernel release unique.
    """

    debian_source_version: DebianVersion
    dkc_revision: int
    build_id: str

    def __post_init__(self) -> None:
        if self.dkc_revision < 1:
            raise InvalidIdentity(
                f"DKC revision must be a positive integer, got {self.dkc_revision}"
            )
        if not _BUILD_ID_RE.match(self.build_id):
            raise InvalidIdentity(
                f"build id must be at least 12 lowercase hex characters, got {self.build_id!r}"
            )

    @classmethod
    def create(
        cls, debian_source_version: str, dkc_revision: int, build_id: str
    ) -> Identity:
        return cls(
            debian_source_version=DebianVersion.parse(debian_source_version),
            dkc_revision=dkc_revision,
            build_id=build_id,
        )

    @property
    def upstream_release(self) -> str:
        return self.debian_source_version.upstream_release

    @property
    def series(self) -> tuple[int, int]:
        return self.debian_source_version.series

    @property
    def package_version(self) -> str:
        """The `dkc-linux` source and binary version.

        Formed by appending to the full Debian version, including its revision,
        so that DKC versions of successive Debian revisions order correctly and
        a DKC version always sorts above the Debian version it derives from.
        The epoch, if any, is preserved at the front where Debian expects it.
        """
        version = f"{self.debian_source_version.raw}+{_DKC_SUFFIX}.{self.dkc_revision}"
        # Re-parse so an ill-formed result is caught here rather than by dpkg
        # halfway through a build.
        DebianVersion.parse(version)
        return version

    @property
    def abi(self) -> str:
        """The ABI name shared by all flavors of this publication.

        Contains no epoch and no character that is unsafe in a path.
        """
        return (
            f"{self.upstream_release}+{_DKC_SUFFIX}"
            f".r{self.dkc_revision}.g{self.build_id}"
        )

    def kernel_release(self, flavor: str) -> str:
        """The `KREL`: `uname -r`, `/lib/modules/<KREL>`, `/boot/vmlinuz-<KREL>`."""
        if flavor not in FLAVORS:
            raise InvalidIdentity(f"unknown flavor {flavor!r}, expected one of {FLAVORS}")
        krel = f"{self.abi}-{flavor}-amd64"
        _validate_kernel_release(krel)
        return krel


def _validate_kernel_release(krel: str) -> None:
    if ":" in krel:
        raise InvalidIdentity(f"kernel release must not contain an epoch: {krel!r}")
    if "/" in krel or krel in (".", ".."):
        raise InvalidIdentity(f"kernel release is not a safe path component: {krel!r}")
    if not _KREL_RE.match(krel):
        raise InvalidIdentity(f"kernel release contains an unsafe character: {krel!r}")
    if len(krel.encode()) > UTS_RELEASE_MAX:
        raise InvalidIdentity(
            f"kernel release is {len(krel.encode())} bytes, over the "
            f"{UTS_RELEASE_MAX}-byte UTS_RELEASE limit: {krel!r}"
        )


def package_names(identity: Identity) -> dict[str, list[str]]:
    """Every binary package name this publication produces.

    Mirrors the roles of the Debian kernel package graph under the `dkc-`
    namespace, rather than collapsing it into an ad-hoc image/modules split.
    Meta-package names are flavor-scoped and version-independent, so they are
    the stable upgrade path; everything else is pinned to the ABI.
    """
    abi = identity.abi
    versioned: list[str] = []
    for flavor in FLAVORS:
        suffix = f"{flavor}-amd64"
        versioned += [
            f"dkc-linux-base-{abi}-{suffix}",
            f"dkc-linux-binary-{abi}-{suffix}",
            f"dkc-linux-modules-{abi}-{suffix}",
            f"dkc-linux-image-{abi}-{suffix}",
            f"dkc-linux-headers-{abi}-{suffix}",
        ]
    versioned += [
        f"dkc-linux-headers-{abi}-common",
        f"dkc-linux-kbuild-{abi}",
    ]

    meta = [f"dkc-linux-base-{flavor}-amd64" for flavor in FLAVORS]
    meta += [f"dkc-linux-image-{flavor}-amd64" for flavor in FLAVORS]
    meta += [f"dkc-linux-headers-{flavor}-amd64" for flavor in FLAVORS]

    return {"versioned": versioned, "meta": meta, "keyring": ["dkc-archive-keyring"]}

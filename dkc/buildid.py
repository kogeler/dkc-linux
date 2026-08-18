"""The publication-wide build-input digest and the `BUILD_ID` derived from it.

Two publications with different inputs must get different identities, and one
publication's three flavors must share one identity. The digest therefore covers
the whole ordered inventory of inputs, not just the compiler version.

The circularity trap this module exists to avoid: the kernel release contains
the build id, the configuration contains the kernel release, and the digest
covers the configuration. Hashing a configuration that already carries an
identity field would make the identity depend on itself. So the digest is taken
over the *pre-identity* policy configuration. The build path independently
reconstructs the final configuration and compares it byte-for-byte with the
configuration consumed by the compiler.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

from .serialize import canonical_bytes

__all__ = [
    "BuildInputs",
    "BUILD_ID_LENGTH",
    "IDENTITY_FIELDS",
    "normalized_policy_config",
    "policy_config_digest",
]

# At least 12 lowercase hex characters of the digest are displayed.
BUILD_ID_LENGTH = 12

# The only configuration keys allowed to differ between the pre-identity policy
# configuration and the final configuration. Anything else differing means the
# build changed something it should not have.
IDENTITY_FIELDS: frozenset[str] = frozenset(
    {
        "CONFIG_LOCALVERSION",
        "CONFIG_BUILD_SALT",
    }
)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_IMAGE_DIGEST_RE = re.compile(r"^[a-z0-9./_-]+@sha256:[0-9a-f]{64}$")
_DEPENDENCY_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9+.-]*:[a-z0-9][a-z0-9-]*$")
_LOCKED_DEPENDENCY_RE = re.compile(r"^\S+@[0-9a-f]{64}$")
_LTO_MODES = frozenset({"none", "thin", "full"})


def normalized_policy_config(config: dict[str, str]) -> dict[str, str]:
    """Return a resolved Kconfig without its derived identity fields."""

    malformed = sorted(key for key in config if not re.fullmatch(r"CONFIG_[A-Z0-9_]+", key))
    if malformed:
        raise ValueError(f"malformed resolved configuration key(s): {malformed}")
    normalized = {
        key: value for key, value in sorted(config.items()) if key not in IDENTITY_FIELDS
    }
    if not normalized:
        raise ValueError("resolved policy configuration must not be empty")
    return normalized


def policy_config_digest(config: dict[str, str]) -> str:
    """Hash a resolved Kconfig after removing only derived identity fields."""

    return hashlib.sha256(canonical_bytes(normalized_policy_config(config))).hexdigest()


@dataclass(frozen=True)
class BuildInputs:
    """The canonical inventory the publication identity is derived from."""

    schema_version: int
    debian_source_version: str
    dsc_sha256: str
    source_member_sha256: dict[str, str]
    dkc_revision: int
    overlay_sha256: str
    flavor_config_sha256: dict[str, str]
    flavor_policy: dict[str, str]
    base_image_digest: str
    toolchain_lock_sha256: str
    build_policy_revision: int
    lto_mode: str
    dependency_lock: dict[str, str] = field(default_factory=dict)

    def inventory(self) -> dict[str, object]:
        """The exact structure that gets hashed.

        Flavors are emitted in a fixed order rather than whatever order a caller
        happened to iterate, because the digest must not depend on that.
        """
        flavor_order = ("v2", "v3", "v4")
        expected = set(flavor_order)
        config_keys = set(self.flavor_config_sha256)
        policy_keys = set(self.flavor_policy)
        if config_keys != expected:
            raise ValueError(
                "configuration hashes must cover exactly v2, v3 and v4; "
                f"missing={sorted(expected - config_keys)}, "
                f"unexpected={sorted(config_keys - expected)}"
            )
        if policy_keys != expected:
            raise ValueError(
                "flavor policy must cover exactly v2, v3 and v4; "
                f"missing={sorted(expected - policy_keys)}, "
                f"unexpected={sorted(policy_keys - expected)}"
            )
        hashes = {
            "dsc_sha256": self.dsc_sha256,
            "overlay_sha256": self.overlay_sha256,
            "toolchain_lock_sha256": self.toolchain_lock_sha256,
            **{f"source member {name}": digest for name, digest in self.source_member_sha256.items()},
            **{f"{flavor} config": digest for flavor, digest in self.flavor_config_sha256.items()},
        }
        malformed = sorted(name for name, digest in hashes.items() if not _SHA256_RE.fullmatch(digest))
        if malformed:
            raise ValueError(f"malformed SHA-256 input(s): {malformed}")
        if self.schema_version < 1 or self.build_policy_revision < 1 or self.dkc_revision < 1:
            raise ValueError("schema, build-policy and DKC revisions must be positive")
        if self.lto_mode not in _LTO_MODES:
            raise ValueError("LTO mode must be none, thin, or full")
        if not self.debian_source_version or len(self.source_member_sha256) != 2:
            raise ValueError("source identity must name a version and exactly two source members")
        if not _IMAGE_DIGEST_RE.fullmatch(self.base_image_digest):
            raise ValueError("base image must be an immutable registry SHA-256 digest")
        if not self.dependency_lock:
            raise ValueError("exact build dependency lock must not be empty")
        malformed_dependencies = sorted(
            key
            for key, value in self.dependency_lock.items()
            if not _DEPENDENCY_KEY_RE.fullmatch(key)
            or not _LOCKED_DEPENDENCY_RE.fullmatch(value)
        )
        if malformed_dependencies:
            raise ValueError(
                f"malformed exact build dependency record(s): {malformed_dependencies}"
            )

        return {
            "schema_version": self.schema_version,
            "build_policy_revision": self.build_policy_revision,
            "debian_source": {
                "version": self.debian_source_version,
                "dsc_sha256": self.dsc_sha256,
                "members": dict(sorted(self.source_member_sha256.items())),
            },
            "dkc_revision": self.dkc_revision,
            "lto_mode": self.lto_mode,
            "overlay_sha256": self.overlay_sha256,
            "flavors": [
                {
                    "flavor": flavor,
                    "config_sha256": self.flavor_config_sha256[flavor],
                    "policy": self.flavor_policy[flavor],
                }
                for flavor in flavor_order
            ],
            "toolchain": {
                # Immutable FROM digest. The build-container recipe is covered
                # by overlay_sha256 and every installed package byte by the
                # lock/dependencies below; an engine-local image ID is not used
                # because its layer timestamps differ across fresh CI jobs.
                "base_image_digest": self.base_image_digest,
                "lock_sha256": self.toolchain_lock_sha256,
                "dependencies": dict(sorted(self.dependency_lock.items())),
            },
        }

    def digest(self) -> str:
        """The full publication digest, lowercase hex."""
        return hashlib.sha256(canonical_bytes(self.inventory())).hexdigest()

    def build_id(self, length: int = BUILD_ID_LENGTH) -> str:
        if length < BUILD_ID_LENGTH:
            raise ValueError(f"build id must be at least {BUILD_ID_LENGTH} characters")
        return self.digest()[:length]

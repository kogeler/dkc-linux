"""The exact versioned policy inputs that can affect a kernel publication."""

from __future__ import annotations

import hashlib
import pathlib

from .sourceprofile import select_profile

__all__ = ["BUILD_POLICY_REVISION", "build_policy_digest", "build_policy_paths"]


BUILD_POLICY_REVISION = 9


def build_policy_paths(
    root: pathlib.Path, source_version: str
) -> tuple[pathlib.Path, ...]:
    """Return every tracked input embedded in and hashed by the source package.

    Only the source profile that covers `source_version` contributes, so adding
    or revising another kernel series never changes this publication identity.
    """

    relative = [
        "config/base-image.lock",
        "config/toolchain.env",
        "container/Containerfile.build",
        "container/build-patched-packages.py",
        "container/patched-packages.toml",
        "debian-overlay/edits.py",
        "dkc/__init__.py",
        "dkc/buildid.py",
        "dkc/buildpolicy.py",
        "dkc/cli/__init__.py",
        "dkc/cli/audit_source_package.py",
        "dkc/cli/build_source_package.py",
        "dkc/cli/generate_overlay_patches.py",
        "dkc/cli/normalize_quilt_patch.py",
        "dkc/cli/prepare_attestation_replay.py",
        "dkc/cli/prepare_build_identity.py",
        "dkc/cli/prepare_flavor.py",
        "dkc/cli/prepare_source_tree.py",
        "dkc/cli/run_one_build.py",
        "dkc/cli/verify_replay_elf.py",
        "dkc/debver.py",
        "dkc/flavors.py",
        "dkc/naming.py",
        "dkc/overlay.py",
        "dkc/serialize.py",
        "dkc/sourcepackage.py",
        "dkc/sourceprofile.py",
        "dkc/tarmetadata.py",
        "scripts/build-one.sh",
        "scripts/in-container/build-source-package.sh",
        "scripts/in-container/prepare-attestation-replay.sh",
        "scripts/in-container/run-one-build.sh",
        "scripts/lib/podman-image.sh",
    ]
    relative.extend(
        path.relative_to(root).as_posix()
        for path in select_profile(root, source_version).build_policy_paths()
    )
    relative.extend(
        path.relative_to(root).as_posix()
        for path in sorted((root / "debian-overlay/source").glob("*"))
        if path.is_file()
    )
    relative.extend(
        path.relative_to(root).as_posix()
        for path in sorted((root / "config/flavors").glob("*.toml"))
    )
    paths = tuple(root / name for name in sorted(set(relative)))
    missing = [
        path.relative_to(root).as_posix() for path in paths if not path.is_file()
    ]
    if missing:
        raise ValueError(f"build-policy inputs are absent: {missing}")
    return paths


def build_policy_digest(root: pathlib.Path, source_version: str) -> str:
    """Hash relative names and bytes so renames are identity changes too."""

    digest = hashlib.sha256()
    for path in build_policy_paths(root, source_version):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        payload = path.read_bytes()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return digest.hexdigest()

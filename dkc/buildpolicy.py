"""The exact versioned policy inputs that can affect a kernel publication."""

from __future__ import annotations

import hashlib
import pathlib

__all__ = ["BUILD_POLICY_REVISION", "build_policy_digest", "build_policy_paths"]


BUILD_POLICY_REVISION = 7


def build_policy_paths(root: pathlib.Path) -> tuple[pathlib.Path, ...]:
    """Return every tracked input embedded in and hashed by the source package."""

    relative = [
        "dkc/buildid.py",
        "dkc/buildpolicy.py",
        "dkc/flavors.py",
        "dkc/naming.py",
        "dkc/serialize.py",
        "dkc/sourcepackage.py",
        "config/base-image.lock",
        "config/build-profiles",
        "container/Containerfile.build",
        "scripts/build-one.sh",
        "scripts/lib/podman-image.sh",
        "scripts/in-container/audit-source-package.py",
        "scripts/in-container/build-source-package.sh",
        "scripts/in-container/generate-overlay-patches.py",
        "scripts/in-container/normalize-quilt-patch.py",
        "scripts/in-container/prepare-build-identity.py",
        "scripts/in-container/prepare-attestation-replay.sh",
        "scripts/in-container/verify-replay-elf.py",
        "scripts/in-container/prepare-flavor.py",
        "scripts/in-container/prepare-source-tree.py",
        "scripts/in-container/run-one-build.sh",
    ]
    relative.extend(
        path.relative_to(root).as_posix()
        for path in sorted((root / "debian-overlay/patches").glob("*.patch"))
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
    missing = [path.relative_to(root).as_posix() for path in paths if not path.is_file()]
    if missing:
        raise ValueError(f"build-policy inputs are absent: {missing}")
    return paths


def build_policy_digest(root: pathlib.Path) -> str:
    """Hash relative names and bytes so renames are identity changes too."""

    digest = hashlib.sha256()
    for path in build_policy_paths(root):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        payload = path.read_bytes()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    return digest.hexdigest()

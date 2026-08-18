"""Tracked inputs that determine whether a built flavor is accepted."""

from __future__ import annotations

import hashlib
import pathlib

__all__ = ["validation_policy_digest", "validation_policy_paths"]


def validation_policy_paths(root: pathlib.Path) -> tuple[pathlib.Path, ...]:
    """Return acceptance inputs that must invalidate a qualified result."""

    relative = [
        "config/kselftest.env",
        "config/qemu-cpus.env",
        "config/qemu-image.env",
        "dkc/evidence.py",
        "dkc/release_cache.py",
        "dkc/validationpolicy.py",
        "mk/vm.mk",
        "scripts/build-kselftest-flavor.sh",
        "scripts/dkc-cpu-select",
        "scripts/fetch-qemu-image.sh",
        "scripts/github-prepare-kvm.sh",
        "scripts/in-container/attest-one-build.py",
        "scripts/in-container/audit-kbuild-commands.py",
        "scripts/in-container/audit-kernel-simd.py",
        "scripts/in-container/build-kselftest-flavor.sh",
        "scripts/in-container/build-kselftest.sh",
        "scripts/in-container/finalize-one-build.sh",
        "scripts/in-container/lock-build-environment.py",
        "scripts/in-container/prepare-attestation-replay.sh",
        "scripts/in-container/prepare-qemu-inputs.sh",
        "scripts/in-container/stage-one-build.sh",
        "scripts/in-container/verify-replay-elf.py",
        "scripts/lib/common.sh",
        "scripts/lib/podman-image.sh",
        "scripts/qemu-boot.sh",
        "scripts/qemu-preflight.sh",
    ]
    for directory in (
        root / "tests/integration/dkms-fixture",
        root / "tests/integration/kselftest-patches",
        root / "tests/integration/kselftest-wrappers",
        root / "tests/integration/qemu",
    ):
        relative.extend(
            path.relative_to(root).as_posix()
            for path in sorted(directory.rglob("*"))
            if path.is_file()
        )
    paths = tuple(root / name for name in sorted(set(relative)))
    missing = [path.relative_to(root).as_posix() for path in paths if not path.is_file()]
    if missing:
        raise ValueError(f"validation-policy inputs are absent: {missing}")
    return paths


def validation_policy_digest(root: pathlib.Path) -> str:
    digest = hashlib.sha256()
    for path in validation_policy_paths(root):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        size = path.stat().st_size
        digest.update(size.to_bytes(8, "big"))
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    return digest.hexdigest()

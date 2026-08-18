from __future__ import annotations

import shutil
from pathlib import Path

from dkc.validationpolicy import validation_policy_digest, validation_policy_paths


ROOT = Path(__file__).resolve().parents[1]


def test_validation_policy_is_complete_but_independent_from_container_images(
    tmp_path: Path,
) -> None:
    policy_paths = validation_policy_paths(ROOT)
    paths = {path.relative_to(ROOT).as_posix() for path in policy_paths}
    assert {
        "dkc/release_cache.py",
        "dkc/validationpolicy.py",
        "scripts/in-container/audit-kernel-simd.py",
        "scripts/in-container/build-kselftest.sh",
        "scripts/in-container/lock-build-environment.py",
        "scripts/qemu-boot.sh",
        "tests/integration/qemu/guest-validate.sh",
    } <= paths
    assert not any(path.startswith("container/") for path in paths)
    assert not any(path.startswith(".github/") for path in paths)
    original_digest = validation_policy_digest(ROOT)
    assert len(original_digest) == 64

    for source in policy_paths:
        destination = tmp_path / source.relative_to(ROOT)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    assert validation_policy_digest(tmp_path) == original_digest
    with (tmp_path / "config/qemu-cpus.env").open("a") as stream:
        stream.write("\n")
    assert validation_policy_digest(tmp_path) != original_digest
